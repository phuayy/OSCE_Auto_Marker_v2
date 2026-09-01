"""The settings API surface behind the engine picker.

The screen renders itself from GET /api/settings/transcription-engines and
saves through PUT /api/settings, so this suite pins the response shape the form
depends on and the validation that keeps an unusable option from being stored
and only failing an hour later inside a run.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import settings as settings_routes
from app.core.config import Settings
from app.services.container import create_container


def build_client(tmp_path: Path, **setting_overrides) -> TestClient:
    settings = Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        app_database_url="",
        database_url="",
        **setting_overrides,
    )
    container = create_container(settings)
    asyncio.run(container.artifacts.ensure_storage_layout())

    app = FastAPI()
    app.state.container = container
    app.include_router(settings_routes.router, prefix="/api")
    return TestClient(app)


def save(client: TestClient, **overrides):
    payload = {"llmTranscriptPreprocess": False, **overrides}
    return client.put("/api/settings", json=payload)


def test_the_engine_list_describes_everything_the_form_renders(tmp_path: Path) -> None:
    body = build_client(tmp_path).get("/api/settings/transcription-engines").json()

    engines = {engine["id"]: engine for engine in body["engines"]}
    assert set(engines) == {"whisperx", "canary-qwen"}
    whisperx = engines["whisperx"]
    assert whisperx["label"] == "WhisperX"
    assert whisperx["capabilities"]["diarization"] is True
    assert whisperx["defaults"]["model"] == "large-v3"
    assert "available" in whisperx["availability"]
    parameter_names = {parameter["name"] for parameter in whisperx["parameters"]}
    assert {"model", "batchSize", "chunkSize", "minSpeakers", "maxSpeakers"} <= parameter_names


def test_parameters_publish_their_type_and_bounds(tmp_path: Path) -> None:
    body = build_client(tmp_path).get("/api/settings/transcription-engines").json()
    canary = next(engine for engine in body["engines"] if engine["id"] == "canary-qwen")

    chunk = next(parameter for parameter in canary["parameters"] if parameter["name"] == "chunkSeconds")
    device = next(parameter for parameter in canary["parameters"] if parameter["name"] == "device")

    assert chunk["type"] == "float" and chunk["maximum"] == 40.0
    assert device["type"] == "enum" and device["options"] == ["auto", "cuda", "cpu"]
    assert canary["capabilities"]["diarization"] is False
    assert canary["requirements"]


def test_defaults_reflect_this_deployment_not_the_schema(tmp_path: Path) -> None:
    client = build_client(tmp_path, whisperx_model="distil-large-v3", whisperx_chunk_size=15)

    body = client.get("/api/settings/transcription-engines").json()
    whisperx = next(engine for engine in body["engines"] if engine["id"] == "whisperx")

    assert whisperx["defaults"]["model"] == "distil-large-v3"
    assert whisperx["defaults"]["chunkSize"] == 15


def test_selecting_an_engine_persists_and_is_reported_back(tmp_path: Path) -> None:
    client = build_client(tmp_path)

    response = save(
        client,
        transcriptionEngine="canary-qwen",
        transcriptionEngineOptions={"canary-qwen": {"chunkSeconds": 25}},
    )

    assert response.status_code == 200
    stored = client.get("/api/settings").json()["settings"]
    assert stored["transcriptionEngine"] == "canary-qwen"
    assert stored["transcriptionEngineOptions"]["canary-qwen"]["chunkSeconds"] == 25.0
    assert client.get("/api/settings/transcription-engines").json()["selected"] == {
        "engineId": "canary-qwen",
        "options": {"chunkSeconds": 25.0},
    }


def test_options_are_coerced_to_their_declared_types(tmp_path: Path) -> None:
    client = build_client(tmp_path)

    save(
        client,
        transcriptionEngine="whisperx",
        transcriptionEngineOptions={"whisperx": {"batchSize": "4", "chunkSize": "15"}},
    )

    stored = client.get("/api/settings").json()["settings"]["transcriptionEngineOptions"]["whisperx"]
    assert stored == {"batchSize": 4, "chunkSize": 15}


def test_an_unknown_engine_is_refused(tmp_path: Path) -> None:
    client = build_client(tmp_path)

    assert save(client, transcriptionEngine="whisper-turbo-9000").status_code == 422
    assert save(client, transcriptionEngineOptions={"nope": {}}).status_code == 422


def test_an_out_of_range_option_is_refused_at_the_form(tmp_path: Path) -> None:
    client = build_client(tmp_path)

    response = save(client, transcriptionEngineOptions={"canary-qwen": {"chunkSeconds": 900}})

    assert response.status_code == 422
    assert "Chunk length" in response.text


def test_an_unknown_option_name_is_refused(tmp_path: Path) -> None:
    client = build_client(tmp_path)

    response = save(client, transcriptionEngineOptions={"whisperx": {"modle": "large-v3"}})

    assert response.status_code == 422
    assert "not an option" in response.text


def test_options_for_other_engines_survive_a_switch(tmp_path: Path) -> None:
    # Tuning done for one engine is kept when the operator tries another and
    # comes back — the reason options are stored per engine.
    client = build_client(tmp_path)
    save(client, transcriptionEngine="whisperx", transcriptionEngineOptions={"whisperx": {"batchSize": 4}})

    save(
        client,
        transcriptionEngine="canary-qwen",
        transcriptionEngineOptions={"whisperx": {"batchSize": 4}, "canary-qwen": {"batchSize": 2}},
    )

    stored = client.get("/api/settings").json()["settings"]["transcriptionEngineOptions"]
    assert stored["whisperx"]["batchSize"] == 4
    assert stored["canary-qwen"]["batchSize"] == 2


def test_an_empty_selection_means_the_deployment_default(tmp_path: Path) -> None:
    client = build_client(tmp_path, transcription_engine="canary-qwen")

    assert save(client, transcriptionEngine="").status_code == 200
    body = client.get("/api/settings/transcription-engines").json()
    assert body["selected"]["engineId"] == "canary-qwen"
    assert body["defaultEngineId"] == "canary-qwen"
