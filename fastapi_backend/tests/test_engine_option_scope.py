"""Engine options that decide what the *server* runs are an administrator's.

``audioFilters`` is an ffmpeg filter graph and ``model`` names a checkpoint
repo or path; both used to be writable by any marker through the personal
``transcriptionEngineOptions`` override. Two layers now guard them: a
per-parameter scope (``ParameterSpec.deployment_scoped``) enforced on the write
path and again when a user's row is layered over the deployment document, and
a validator on the filter chain itself that applies to everyone.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.config import Settings
from app.pipeline.audio_filters import validate_filter_chain
from app.pipeline.transcription import registry
from app.pipeline.transcription.base import TranscriptionOptionError, validate_options
from app.pipeline.transcription.whisperx_engine import DESCRIPTOR as WHISPERX

from tests.test_routes import build_test_client
from tests.test_user_admin import GOOD_PASSWORD, MARKER_EMAIL, _activate_marker, _headers, _token

GRAPH = "amovie=/etc/passwd[x];[in][x]amix"


# --- the filter chain validator ----------------------------------------------


@pytest.mark.parametrize(
    "chain",
    [
        "",
        "   ",
        "highpass=f=80,loudnorm",
        "highpass=f=80,loudnorm=I=-16:TP=-1.5:LRA=11",
        "equalizer=f=1000:t=q:w=1:g=-3,volume=0.5",
        "pan=mono|c0=0.5*c0+0.5*c1,aformat=sample_fmts=s16",
        "compand=attacks=0:points=-80/-80|-20/-20",
        "dynaudnorm",
    ],
)
def test_safe_chains_pass(chain: str) -> None:
    assert validate_filter_chain(chain) == chain.strip()


@pytest.mark.parametrize(
    "chain",
    [
        GRAPH,
        "amovie=/etc/passwd",
        "movie=http://169.254.169.254/",
        "ladspa=file=evil",
        "highpass=f=80;loudnorm",
        "[in]highpass=f=80[out]",
        "highpass=f='80'",
        'highpass=f="80"',
        "highpass=f=80 ,loudnorm",
        "highpass=",
        "highpass=f=80,,loudnorm",
        "highpass=f=80\nloudnorm",
        "a" * 600,
    ],
)
def test_unsafe_chains_are_refused(chain: str) -> None:
    with pytest.raises(ValueError):
        validate_filter_chain(chain)


def test_the_default_chain_and_both_env_defaults_pass_the_validator() -> None:
    spec = WHISPERX.parameter("audioFilters")
    assert spec is not None
    validate_filter_chain(str(spec.default))
    validate_filter_chain(Settings().canary_audio_filters)


def test_the_option_boundary_refuses_a_graph_and_accepts_unfiltered() -> None:
    with pytest.raises(TranscriptionOptionError, match="not allowed"):
        validate_options(WHISPERX, {"audioFilters": GRAPH})
    assert validate_options(WHISPERX, {"audioFilters": ""}) == {"audioFilters": ""}
    assert validate_options(WHISPERX, {"audioFilters": "highpass=f=100"}) == {"audioFilters": "highpass=f=100"}


def test_a_blank_model_is_refused_rather_than_passed_to_the_cli() -> None:
    with pytest.raises(TranscriptionOptionError, match="must not be empty"):
        validate_options(WHISPERX, {"model": "  "})


def test_a_stored_invalid_chain_degrades_to_the_defaults(tmp_path: Path) -> None:
    from tests.test_transcription_settings_api import build_client  # noqa: F401  (harness parity)

    container = build_client(tmp_path).app.state.container
    engine = container.transcription.engines["whisperx"]
    options = container.transcription.safe_options(engine, {"audioFilters": GRAPH, "batchSize": 4})
    assert options["audioFilters"] == engine.default_options()["audioFilters"]


def test_an_invalid_env_chain_is_flagged_at_startup() -> None:
    warnings = Settings(whisperx_audio_filters=GRAPH).collect_runtime_warnings()
    assert any("WHISPERX_AUDIO_FILTERS" in warning for warning in warnings)
    assert not any("AUDIO_FILTERS" in warning for warning in Settings().collect_runtime_warnings())


# --- which parameters are deployment-scoped ----------------------------------


def test_host_affecting_parameters_are_marked_and_tuning_ones_are_not() -> None:
    scoped = {
        (engine_id, spec.name)
        for engine_id, descriptor in registry.DESCRIPTORS.items()
        for spec in descriptor.parameters
        if spec.deployment_scoped
    }
    assert scoped == {("whisperx", "model"), ("whisperx", "audioFilters"), ("canary-qwen", "model")}
    assert registry.deployment_scoped_options(
        {"whisperx": {"audioFilters": "x", "minSpeakers": 3}, "canary-qwen": {"model": "m"}}
    ) == ["whisperx.audioFilters", "canary-qwen.model"]
    assert registry.strip_deployment_scoped({"whisperx": {"audioFilters": "x", "minSpeakers": 3}, "bad": 1}) == {
        "whisperx": {"minSpeakers": 3}
    }


def test_the_scope_is_published_on_each_parameter(tmp_path: Path) -> None:
    from tests.test_transcription_settings_api import build_client

    body = build_client(tmp_path).get("/api/settings/transcription-engines").json()
    whisperx = next(engine for engine in body["engines"] if engine["id"] == "whisperx")
    scope = {parameter["name"]: parameter["deploymentScoped"] for parameter in whisperx["parameters"]}
    assert scope["audioFilters"] is True and scope["model"] is True
    assert scope["batchSize"] is False and scope["initialPrompt"] is False


# --- the write path ----------------------------------------------------------


def _marker(tmp_path: Path):
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    _activate_marker(client, admin_token, MARKER_EMAIL)
    return client, admin_token, _token(client, MARKER_EMAIL, GOOD_PASSWORD)


@pytest.mark.parametrize("option", [{"audioFilters": GRAPH}, {"audioFilters": "highpass=f=80"}, {"model": "evil/repo"}])
def test_a_marker_cannot_store_a_deployment_scoped_option(tmp_path: Path, option: dict) -> None:
    client, _, marker_token = _marker(tmp_path)
    body = {"transcriptionEngineOptions": {"whisperx": option}}

    for method in ("patch", "put"):
        payload = {**body, "llmTranscriptPreprocess": False} if method == "put" else body
        response = getattr(client, method)("/api/settings", json=payload, headers=_headers(marker_token))
        assert response.status_code in (400, 403), response.text
        if response.status_code == 403:
            assert "deployment setting" in response.json()["detail"]

    # Nothing reached the marker's row.
    assert client.get("/api/settings", headers=_headers(marker_token)).json()["overrides"] == {}


def test_a_valid_chain_from_a_marker_is_still_a_403(tmp_path: Path) -> None:
    client, _, marker_token = _marker(tmp_path)
    response = client.patch(
        "/api/settings",
        json={"transcriptionEngineOptions": {"whisperx": {"audioFilters": "highpass=f=80"}}},
        headers=_headers(marker_token),
    )
    assert response.status_code == 403
    assert "whisperx.audioFilters" in response.json()["detail"]


def test_a_marker_may_still_tune_user_scoped_options(tmp_path: Path) -> None:
    client, _, marker_token = _marker(tmp_path)
    response = client.patch(
        "/api/settings",
        json={"transcriptionEngineOptions": {"whisperx": {"maxSpeakers": 3, "initialPrompt": "OSCE"}}},
        headers=_headers(marker_token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["settings"]["transcriptionEngineOptions"]["whisperx"]["maxSpeakers"] == 3


def test_an_admin_sets_the_deployment_chain_and_a_graph_is_refused_for_them_too(tmp_path: Path) -> None:
    client, admin_token, _ = _marker(tmp_path)
    ok = client.patch(
        "/api/admin/settings",
        json={"transcriptionEngineOptions": {"whisperx": {"audioFilters": "highpass=f=100,loudnorm", "model": "distil-large-v3"}}},
        headers=_headers(admin_token),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["settings"]["transcriptionEngineOptions"]["whisperx"]["audioFilters"] == "highpass=f=100,loudnorm"

    for bad in (GRAPH, "highpass=f=80;loudnorm", "[a]highpass[b]"):
        refused = client.patch(
            "/api/admin/settings",
            json={"transcriptionEngineOptions": {"whisperx": {"audioFilters": bad}}},
            headers=_headers(admin_token),
        )
        assert refused.status_code == 400, refused.text
    stored = client.get("/api/settings", headers=_headers(admin_token)).json()["defaults"]
    assert stored["transcriptionEngineOptions"]["whisperx"]["audioFilters"] == "highpass=f=100,loudnorm"


def test_an_empty_deployment_chain_still_means_unfiltered(tmp_path: Path) -> None:
    client, admin_token, _ = _marker(tmp_path)
    response = client.patch(
        "/api/admin/settings",
        json={"transcriptionEngineOptions": {"whisperx": {"audioFilters": ""}}},
        headers=_headers(admin_token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["settings"]["transcriptionEngineOptions"]["whisperx"]["audioFilters"] == ""


# --- the read / merge path ---------------------------------------------------


def test_a_row_stored_before_the_rule_is_ignored_when_layered(tmp_path: Path) -> None:
    client, admin_token, marker_token = _marker(tmp_path)
    container = client.app.state.container
    marker_id = next(
        user["id"] for user in client.get("/api/admin/users", headers=_headers(admin_token)).json()["users"]
        if user["email"] == MARKER_EMAIL
    )
    client.patch(
        "/api/admin/settings",
        json={"transcriptionEngineOptions": {"whisperx": {"audioFilters": "highpass=f=100", "model": "distil-large-v3"}}},
        headers=_headers(admin_token),
    )
    # Written straight to the table, the way a pre-change deployment holds it.
    asyncio.run(
        container.user_settings.set_overrides(
            marker_id,
            {"transcriptionEngineOptions": {"whisperx": {"audioFilters": GRAPH, "model": "evil/repo", "maxSpeakers": 3}}},
        )
    )

    engine_id, stored = asyncio.run(container.preferences.transcription_selection(marker_id))
    options = asyncio.run(container.transcription.stored_options("whisperx", marker_id))
    resolved = container.transcription.safe_options(container.transcription.engines["whisperx"], options)

    assert options["audioFilters"] == "highpass=f=100"
    assert options["model"] == "distil-large-v3"
    assert resolved["maxSpeakers"] == 3
    assert resolved["audioFilters"] == "highpass=f=100"
    assert resolved["model"] == "distil-large-v3"


def test_a_markers_tuning_does_not_discard_the_deployment_options(tmp_path: Path) -> None:
    client, admin_token, marker_token = _marker(tmp_path)
    client.patch(
        "/api/admin/settings",
        json={"transcriptionEngineOptions": {"whisperx": {"model": "distil-large-v3", "batchSize": 2}}},
        headers=_headers(admin_token),
    )
    client.patch(
        "/api/settings",
        json={"transcriptionEngineOptions": {"whisperx": {"batchSize": 8}}},
        headers=_headers(marker_token),
    )

    effective = client.get("/api/settings", headers=_headers(marker_token)).json()["settings"]
    assert effective["transcriptionEngineOptions"]["whisperx"] == {"model": "distil-large-v3", "batchSize": 8}
