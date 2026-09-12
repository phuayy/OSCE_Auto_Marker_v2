"""The marking plan: what a run does once the operator's choice meets this
machine's credentials.

The contract mirrors the routing filter. A panel the operator selected runs as
a panel only when it can — enough markers with keys here — and otherwise
degrades to single mode *with the reasons attached*, so the settings screen
says so before an assessment proves it. Every marker's environment is cut
from one credential snapshot and names only its own target and key.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.database.orm import OrmDatabase
from app.llm import registry
from app.llm.panel import MarkingMode, TieBreak
from app.llm.routing import ROUTING_ENV_VAR, LLMTarget, RoutingConfig
from app.repositories.app_settings_repository import AppSettingsRepository
from app.services.llm_settings_service import LLMSettingsService

from tests.test_llm_settings_api import build_client

ALL_KEY_VARS = (
    "OPENAI_API_KEY", "NVIDIA_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY",
)

PANEL = {
    "markers": [
        {"providerId": "nvidia", "model": "nvidia/nemotron-3-super"},
        {"providerId": "gemini", "model": "gemini-2.5-pro"},
    ],
    "adjudicator": {"providerId": "deepseek", "model": "deepseek-chat"},
    "tieBreak": "lenient",
}


@pytest.fixture
def clean_env(monkeypatch):
    for name in ALL_KEY_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def build_service(tmp_path: Path, **keys: str) -> LLMSettingsService:
    repository = AppSettingsRepository(OrmDatabase(tmp_path / "settings.sqlite3"))
    return LLMSettingsService(repository, key_overrides=lambda: dict(keys))


def _routing_of(env: dict[str, str]) -> RoutingConfig:
    return RoutingConfig.from_json(env[ROUTING_ENV_VAR], default_provider_id=registry.DEFAULT_PROVIDER_ID)


def test_default_plan_is_single_mode_against_the_ordinary_routing(tmp_path: Path, clean_env) -> None:
    clean_env.setenv("NVIDIA_API_KEY", "nvidia-key")
    service = build_service(tmp_path)

    plan = asyncio.run(service.marking_plan())

    assert plan.mode is MarkingMode.SINGLE
    assert plan.selected_mode is MarkingMode.SINGLE
    assert plan.markers == ()
    assert plan.adjudicator is None
    assert plan.warnings == ()
    assert plan.single_env == asyncio.run(service.subprocess_env())


def test_a_fully_keyed_panel_gets_one_isolated_env_per_target(tmp_path: Path, clean_env) -> None:
    service = build_service(tmp_path, nvidia="nvidia-key", gemini="gemini-key", deepseek="deepseek-key")
    asyncio.run(service.app_settings.set_values({"llmMarkingMode": "panel", "llmPanel": PANEL}))

    plan = asyncio.run(service.marking_plan())

    assert plan.mode is MarkingMode.PANEL
    assert [assignment.key for assignment in plan.markers] == [
        "nvidia__nvidia-nemotron-3-super",
        "gemini__gemini-2-5-pro",
    ]
    nvidia_env, gemini_env = (assignment.llm_env for assignment in plan.markers)
    # Each marker is routed to itself alone — no fallback chain that could land
    # on the other marker's model — and holds only its own credential.
    assert _routing_of(nvidia_env).targets() == (LLMTarget("nvidia", "nvidia/nemotron-3-super"),)
    assert _routing_of(gemini_env).targets() == (LLMTarget("gemini", "gemini-2.5-pro"),)
    assert nvidia_env["NVIDIA_API_KEY"] == "nvidia-key" and "GEMINI_API_KEY" not in nvidia_env
    assert gemini_env["GEMINI_API_KEY"] == "gemini-key" and "NVIDIA_API_KEY" not in gemini_env
    assert "DEEPSEEK_API_KEY" not in nvidia_env and "DEEPSEEK_API_KEY" not in gemini_env

    assert plan.adjudicator is not None
    assert _routing_of(plan.adjudicator.llm_env).targets() == (LLMTarget("deepseek", "deepseek-chat"),)
    assert plan.adjudicator.llm_env["DEEPSEEK_API_KEY"] == "deepseek-key"
    assert plan.tie_break is TieBreak.LENIENT
    assert plan.warnings == ()


def test_a_marker_with_no_key_here_degrades_the_panel_to_single_with_a_reason(
    tmp_path: Path, clean_env
) -> None:
    # Gemini was configured on the operator's laptop; this box has no key for it.
    service = build_service(tmp_path, nvidia="nvidia-key", deepseek="deepseek-key")
    asyncio.run(service.app_settings.set_values({"llmMarkingMode": "panel", "llmPanel": PANEL}))

    plan = asyncio.run(service.marking_plan())

    assert plan.mode is MarkingMode.SINGLE
    assert plan.selected_mode is MarkingMode.PANEL
    assert plan.markers == ()
    assert any("gemini:gemini-2.5-pro has no API key" in reason for reason in plan.warnings)
    assert any("Fewer than two markers" in reason for reason in plan.warnings)
    # The degraded run is the ordinary single-mode routing, not a lone marker.
    assert plan.single is not None
    assert plan.single_env == asyncio.run(service.subprocess_env())


def test_an_adjudicator_with_no_key_runs_the_panel_with_tie_break_only(tmp_path: Path, clean_env) -> None:
    service = build_service(tmp_path, nvidia="nvidia-key", gemini="gemini-key")
    asyncio.run(service.app_settings.set_values(
        {"llmMarkingMode": "panel", "llmPanel": {**PANEL, "tieBreak": "strict"}}
    ))

    plan = asyncio.run(service.marking_plan())

    assert plan.mode is MarkingMode.PANEL
    assert len(plan.markers) == 2
    assert plan.adjudicator is None
    assert plan.tie_break is TieBreak.STRICT
    assert any("Adjudicator deepseek:deepseek-chat has no API key" in reason for reason in plan.warnings)
    assert any("'strict' tie-break" in reason for reason in plan.warnings)


def test_an_incoherent_stored_panel_degrades_rather_than_failing(tmp_path: Path, clean_env) -> None:
    """A row written by a later or earlier release may not validate; the run
    still marks."""
    service = build_service(tmp_path, nvidia="nvidia-key")
    asyncio.run(service.app_settings.set_values(
        {"llmMarkingMode": "panel", "llmPanel": {"markers": PANEL["markers"][:1]}}
    ))

    plan = asyncio.run(service.marking_plan())

    assert plan.mode is MarkingMode.SINGLE
    assert plan.selected_mode is MarkingMode.PANEL
    assert any("not configured correctly" in reason for reason in plan.warnings)


def test_describe_reports_selected_and_effective_marking(tmp_path: Path, clean_env) -> None:
    clean_env.setenv("NVIDIA_API_KEY", "nvidia-key")
    client = build_client(tmp_path)
    saved = client.put("/api/settings", json={
        "llmTranscriptPreprocess": False,
        "llmMarkingMode": "panel",
        "llmPanel": PANEL,
    })
    assert saved.status_code == 200, saved.text

    marking = client.get("/api/settings/llm-providers").json()["marking"]

    assert marking["mode"] == "panel"
    assert marking["modes"] == ["single", "panel"]
    assert marking["tieBreaks"] == ["lenient", "strict", "first_marker"]
    assert marking["selected"]["markers"] == PANEL["markers"]
    assert marking["selected"]["adjudicator"] == PANEL["adjudicator"]
    # Only NVIDIA has a key here, so the effective run is single mode — and the
    # screen is told why before an assessment finds out.
    assert marking["effective"]["mode"] == "single"
    assert marking["effective"]["selectedMode"] == "panel"
    assert any("gemini:gemini-2.5-pro has no API key" in reason for reason in marking["warnings"])
    # Nothing in the block is a credential.
    assert "nvidia-key" not in str(marking)


# --- the write boundary --------------------------------------------------------

def _put(client, **overrides):
    body = {"llmTranscriptPreprocess": False, **overrides}
    return client.put("/api/settings", json=body)


def test_saving_a_panel_round_trips(tmp_path: Path) -> None:
    client = build_client(tmp_path)

    saved = _put(client, llmMarkingMode="panel", llmPanel=PANEL)

    assert saved.status_code == 200, saved.text
    settings = saved.json()["settings"]
    assert settings["llmMarkingMode"] == "panel"
    assert settings["llmPanel"] == PANEL


def test_panel_mode_with_an_incoherent_panel_is_rejected(tmp_path: Path) -> None:
    client = build_client(tmp_path)

    one_marker = _put(client, llmMarkingMode="panel", llmPanel={**PANEL, "markers": PANEL["markers"][:1]})
    assert one_marker.status_code == 422
    assert "at least 2 markers" in one_marker.json()["detail"]

    duplicate = _put(client, llmMarkingMode="panel", llmPanel={**PANEL, "markers": [PANEL["markers"][1]] * 2})
    assert duplicate.status_code == 422
    assert "repeats marker" in duplicate.json()["detail"]

    no_adjudicator = _put(client, llmMarkingMode="panel", llmPanel={**PANEL, "adjudicator": {}})
    assert no_adjudicator.status_code == 422
    assert "adjudicator" in no_adjudicator.json()["detail"]


def test_an_incomplete_panel_can_be_saved_under_single_mode(tmp_path: Path) -> None:
    """Building the panel up is done while single mode is still selected."""
    client = build_client(tmp_path)

    saved = _put(client, llmMarkingMode="single", llmPanel={"markers": PANEL["markers"][:1]})

    assert saved.status_code == 200, saved.text
    assert saved.json()["settings"]["llmPanel"]["markers"] == PANEL["markers"][:1]


def test_unknown_providers_in_the_panel_are_rejected(tmp_path: Path) -> None:
    client = build_client(tmp_path)

    bad_marker = _put(client, llmPanel={**PANEL, "markers": [PANEL["markers"][0], {"providerId": "acme", "model": "x"}]})
    assert bad_marker.status_code == 422
    assert "Unknown LLM provider 'acme'" in bad_marker.json()["detail"]

    bad_adjudicator = _put(client, llmPanel={**PANEL, "adjudicator": {"providerId": "acme", "model": "x"}})
    assert bad_adjudicator.status_code == 422


def test_unknown_mode_and_tie_break_are_rejected_by_shape(tmp_path: Path) -> None:
    client = build_client(tmp_path)

    assert _put(client, llmMarkingMode="debate").status_code == 422
    assert _put(client, llmPanel={**PANEL, "tieBreak": "coin"}).status_code == 422


def test_blank_marker_rows_are_dropped(tmp_path: Path) -> None:
    client = build_client(tmp_path)

    saved = _put(client, llmPanel={**PANEL, "markers": [*PANEL["markers"], {"providerId": "", "model": ""}]})

    assert saved.status_code == 200, saved.text
    assert saved.json()["settings"]["llmPanel"]["markers"] == PANEL["markers"]
