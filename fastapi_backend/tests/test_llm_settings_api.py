"""The settings-screen contract for scoring providers, and the handoff to
subprocesses.

The behaviour that matters here is not "the endpoint returns 200" but that a
stored selection survives a hostile environment: a provider whose key was
removed, a provider a later release dropped, a settings row written before the
router existed. Each of those used to be a failed assessment.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies import require_admin
from app.api.routes import settings as settings_routes
from app.core.config import Settings
from app.database.orm import OrmDatabase
from app.llm import registry
from app.llm.routing import ROUTING_ENV_VAR, LLMTarget, RetryPolicy, RoutingConfig
from app.repositories.app_settings_repository import AppSettingsRepository
from app.services.container import create_container
from app.services.llm_settings_service import LLMSettingsService


def build_service(tmp_path: Path, **keys: str) -> LLMSettingsService:
    repository = AppSettingsRepository(OrmDatabase(tmp_path / "settings.sqlite3"))
    return LLMSettingsService(repository, key_overrides=lambda: dict(keys))


def build_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        app_database_url="",
        database_url="",
    )
    container = create_container(settings)
    asyncio.run(container.artifacts.ensure_storage_layout())

    app = FastAPI()
    app.state.container = container
    app.include_router(settings_routes.router, prefix="/api")
    app.include_router(settings_routes.admin_router, prefix="/api")
    # This module exercises the settings *business logic*, not authorization —
    # that is test_admin_routes_are_guarded.py and test_session_ownership.py.
    # No auth middleware runs in this standalone app, so require_admin is
    # overridden rather than left to 401 every call.
    app.dependency_overrides[require_admin] = lambda: {"sub": "test-admin", "username": "admin", "role": "admin"}
    return TestClient(app)


def test_provider_description_lists_every_registered_provider(tmp_path: Path) -> None:
    client = build_client(tmp_path)

    body = client.get("/api/settings/llm-providers").json()

    assert [provider["id"] for provider in body["providers"]] == registry.provider_ids()
    assert body["defaultProviderId"] == registry.DEFAULT_PROVIDER_ID
    # Every provider carries what the dropdowns need, and never a credential.
    for provider in body["providers"]:
        assert provider["models"], f"{provider['id']} ships no model shortlist"
        assert "availability" in provider
        assert "apiKey" not in provider


def test_saving_a_primary_and_fallback_round_trips(tmp_path: Path) -> None:
    client = build_client(tmp_path)

    saved = client.put(
        "/api/admin/settings",
        json={
            "llmTranscriptPreprocess": False,
            "llmPrimary": {"providerId": "deepseek", "model": "deepseek-chat"},
            "llmFallbacks": [{"providerId": "openai", "model": "gpt-4.1"}],
        },
    )
    assert saved.status_code == 200
    settings = saved.json()["settings"]
    assert settings["llmPrimary"] == {"providerId": "deepseek", "model": "deepseek-chat"}
    assert settings["llmFallbacks"] == [{"providerId": "openai", "model": "gpt-4.1"}]

    described = client.get("/api/settings/llm-providers").json()
    assert described["selected"]["primary"]["providerId"] == "deepseek"


def test_blank_fallback_rows_are_dropped(tmp_path: Path) -> None:
    """The UI sends a placeholder when its fallback dropdown reads "None"."""
    client = build_client(tmp_path)

    saved = client.put(
        "/api/admin/settings",
        json={
            "llmTranscriptPreprocess": False,
            "llmPrimary": {"providerId": "nvidia", "model": ""},
            "llmFallbacks": [{"providerId": "", "model": ""}],
        },
    )
    assert saved.status_code == 200
    assert saved.json()["settings"]["llmFallbacks"] == []


def test_unknown_provider_is_rejected_at_the_api_boundary(tmp_path: Path) -> None:
    client = build_client(tmp_path)

    rejected = client.put(
        "/api/admin/settings",
        json={
            "llmTranscriptPreprocess": False,
            "llmPrimary": {"providerId": "not-a-vendor", "model": "x"},
        },
    )
    assert rejected.status_code == 422


def test_connection_test_reports_a_missing_key_without_raising(tmp_path: Path, monkeypatch) -> None:
    for name in ("OPENAI_API_KEY",):
        monkeypatch.delenv(name, raising=False)
    client = build_client(tmp_path)

    body = client.post(
        "/api/admin/settings/llm-providers/test",
        json={"providerId": "openai", "model": "gpt-4.1"},
    ).json()

    assert body["ok"] is False
    assert "OPENAI_API_KEY" in body["error"]


def test_routing_drops_a_provider_with_no_key_and_promotes_the_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    service = build_service(tmp_path, deepseek="deepseek-key")
    asyncio.run(
        service.app_settings.set_values(
            {
                "llmPrimary": {"providerId": "openai", "model": "gpt-4.1"},
                "llmFallbacks": [{"providerId": "deepseek", "model": "deepseek-chat"}],
            }
        )
    )

    routing = asyncio.run(service.routing())

    assert routing.primary == LLMTarget("deepseek", "deepseek-chat")
    assert routing.fallbacks == ()


def test_routing_keeps_an_unusable_selection_when_nothing_else_is_configured(
    tmp_path: Path, monkeypatch
) -> None:
    """Better a clear "no API key for openai" than a silent "no target"."""
    for name in ("OPENAI_API_KEY", "NVIDIA_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY",
                 "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    service = build_service(tmp_path)
    asyncio.run(
        service.app_settings.set_values({"llmPrimary": {"providerId": "openai", "model": "gpt-4.1"}})
    )

    routing = asyncio.run(service.routing())

    assert routing.primary == LLMTarget("openai", "gpt-4.1")


def test_subprocess_env_carries_routing_and_only_the_keys_it_needs(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    service = build_service(tmp_path)
    asyncio.run(
        service.app_settings.set_values(
            {
                "llmPrimary": {"providerId": "openai", "model": "gpt-4.1"},
                "llmFallbacks": [],
            }
        )
    )

    env = asyncio.run(service.subprocess_env())

    assert env["OPENAI_API_KEY"] == "openai-key"
    # Anthropic is not in the routing, so its key is not handed to the scorer.
    assert "ANTHROPIC_API_KEY" not in env
    restored = RoutingConfig.from_json(
        env[ROUTING_ENV_VAR], default_provider_id=registry.DEFAULT_PROVIDER_ID
    )
    assert restored.primary == LLMTarget("openai", "gpt-4.1")


def test_serialised_routing_never_contains_a_credential(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret-value")
    service = build_service(tmp_path)
    asyncio.run(
        service.app_settings.set_values({"llmPrimary": {"providerId": "openai", "model": "gpt-4.1"}})
    )

    blob = asyncio.run(service.routing()).to_json()

    assert "super-secret-value" not in blob


def test_routing_survives_a_settings_row_that_predates_the_router(tmp_path: Path) -> None:
    service = build_service(tmp_path, nvidia="nvidia-key")

    routing = asyncio.run(service.routing())

    assert routing.primary.provider_id == registry.DEFAULT_PROVIDER_ID


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"primary": None, "fallbacks": "not-a-list"},
        {"primary": {"providerId": ""}, "fallbacks": [{"model": "orphan"}]},
    ],
)
def test_routing_config_tolerates_malformed_stored_values(raw: object) -> None:
    config = RoutingConfig.from_raw(raw, default_provider_id="nvidia")

    assert config.primary.provider_id == "nvidia"
    assert config.fallbacks == ()


def test_retry_policy_from_raw_clamps_nonsense() -> None:
    policy = RetryPolicy.from_raw(
        {"maxAttemptsPerMode": 0, "jitterRatio": 5.0, "initialBackoffSeconds": -3}
    )

    assert policy.max_attempts_per_mode == 1
    assert policy.jitter_ratio == 1.0
    assert policy.initial_backoff_seconds == 0.0
