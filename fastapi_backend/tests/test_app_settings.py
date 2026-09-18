from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies import require_admin
from app.api.routes import settings as settings_routes
from app.core.config import Settings
from app.database.orm import OrmDatabase
from app.repositories.app_settings_repository import AppSettingsRepository
from app.services.container import create_container


def test_settings_repository_roundtrip(tmp_path: Path) -> None:
    repository = AppSettingsRepository(OrmDatabase(tmp_path / "settings.sqlite3"))

    assert asyncio.run(repository.get_all()) == {
        "llmTranscriptPreprocess": False,
        # "" = use whichever engine this deployment configured.
        "transcriptionEngine": "",
        "transcriptionEngineOptions": {},
        # {} / [] = use this deployment's default scoring provider, no fallback.
        "llmPrimary": {},
        "llmFallbacks": [],
        # "single" / {} = one model marks, no panel configured.
        "llmMarkingMode": "single",
        "llmPanel": {},
    }
    assert asyncio.run(repository.llm_preprocess_enabled()) is False
    assert asyncio.run(repository.llm_routing_selection()) == ({}, [])
    assert asyncio.run(repository.marking_selection()) == ("single", {})

    updated = asyncio.run(repository.set_values({"llmTranscriptPreprocess": True}))
    assert updated["llmTranscriptPreprocess"] is True
    assert asyncio.run(repository.llm_preprocess_enabled()) is True

    updated = asyncio.run(repository.set_values({"llmTranscriptPreprocess": False}))
    assert updated["llmTranscriptPreprocess"] is False
    assert asyncio.run(repository.llm_preprocess_enabled()) is False


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
    # This module exercises settings storage, not authorization.
    app.dependency_overrides[require_admin] = lambda: {"sub": "test-admin", "username": "admin", "role": "admin"}
    return TestClient(app)


def test_settings_routes_get_put_and_reject_unknown_keys(tmp_path: Path) -> None:
    client = build_client(tmp_path)

    fetched = client.get("/api/settings")
    assert fetched.status_code == 200
    assert fetched.json()["settings"] == {
        "llmTranscriptPreprocess": False,
        "transcriptionEngine": "",
        "transcriptionEngineOptions": {},
        "llmPrimary": {},
        "llmFallbacks": [],
        "llmMarkingMode": "single",
        "llmPanel": {},
    }

    updated = client.put("/api/admin/settings", json={"llmTranscriptPreprocess": True})
    assert updated.status_code == 200
    assert updated.json()["settings"]["llmTranscriptPreprocess"] is True

    # Persisted — a fresh GET reflects the stored value.
    assert client.get("/api/settings").json()["settings"]["llmTranscriptPreprocess"] is True

    # Unknown keys 422 loudly (extra="forbid") instead of silently dropping.
    rejected = client.put("/api/admin/settings", json={"llmTranscriptPreprocess": True, "bogus": 1})
    assert rejected.status_code == 422
