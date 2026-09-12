from __future__ import annotations

import asyncio

from app.core.config import Settings
from app.database.orm import OrmDatabase
from app.repositories.session_repository import SessionRepository
from app.services.session_service import SessionService


def _make_service(tmp_path) -> SessionService:
    settings = Settings(root_dir=tmp_path, backend_root=tmp_path, ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", scorer_python_bin="python")
    repo = SessionRepository(OrmDatabase(tmp_path / "sessions.sqlite3"))
    return SessionService(settings, repo)


def test_public_session_hides_absolute_paths(tmp_path) -> None:
    service = _make_service(tmp_path)
    public = service.public_session(
        {
            "id": "s1",
            "name": "Case 1",
            "createdAt": "2026-01-01T00:00:00Z",
            "status": "uploaded",
            "pipeline": {},
            "files": {
                "video": {
                    "originalName": "video.mp4",
                    "fileName": "s1-video.mp4",
                    "absolutePath": "C:/secret/video.mp4",
                    "url": "/media/videos/s1-video.mp4",
                    "sizeBytes": 10,
                },
                "caseStudy": None,
            },
            "outputs": {"audio": None, "videoClips": None},
            "error": None,
        }
    )
    assert "absolutePath" not in str(public)
    assert public["files"]["video"]["url"] == "/media/videos/s1-video.mp4"


def test_public_session_forwards_panel_artifacts_without_absolute_paths(tmp_path) -> None:
    service = _make_service(tmp_path)
    public = service.public_session(
        {
            "id": "s1",
            "name": "Case 1",
            "createdAt": "2026-01-01T00:00:00Z",
            "status": "completed",
            "pipeline": {},
            "files": {"video": None, "caseStudy": None},
            "outputs": {
                "audio": None,
                "videoClips": None,
                "scores": {
                    "fileName": "s1.json",
                    "absolutePath": "C:/secret/scores/s1.json",
                    "url": "/media/scores/s1.json",
                    "sizeBytes": 512,
                    "panelArtifacts": {
                        "markers": {
                            "nvidia__nemotron": {
                                "fileName": "nvidia__nemotron.json",
                                "absolutePath": "C:/secret/scores/panel/s1/nvidia__nemotron.json",
                                "url": "/media/scores/panel/s1/nvidia__nemotron.json",
                                "sizeBytes": 128,
                            },
                            "gemini__gemini-pro": {
                                "fileName": "gemini__gemini-pro.json",
                                "absolutePath": "C:/secret/scores/panel/s1/gemini__gemini-pro.json",
                                "url": "/media/scores/panel/s1/gemini__gemini-pro.json",
                                "sizeBytes": 256,
                            },
                        },
                        "adjudication": {
                            "fileName": "adjudication.json",
                            "absolutePath": "C:/secret/scores/panel/s1/adjudication.json",
                            "url": "/media/scores/panel/s1/adjudication.json",
                            "sizeBytes": 64,
                        },
                    },
                },
            },
            "error": None,
        }
    )
    panel_artifacts = public["outputs"]["scores"]["panelArtifacts"]
    assert set(panel_artifacts["markers"]["nvidia__nemotron"]) == {"fileName", "sizeBytes", "url"}
    assert panel_artifacts["markers"]["nvidia__nemotron"] == {
        "fileName": "nvidia__nemotron.json",
        "sizeBytes": 128,
        "url": "/media/scores/panel/s1/nvidia__nemotron.json",
    }
    assert set(panel_artifacts["markers"]["gemini__gemini-pro"]) == {"fileName", "sizeBytes", "url"}
    assert panel_artifacts["markers"]["gemini__gemini-pro"] == {
        "fileName": "gemini__gemini-pro.json",
        "sizeBytes": 256,
        "url": "/media/scores/panel/s1/gemini__gemini-pro.json",
    }
    assert set(panel_artifacts["adjudication"]) == {"fileName", "sizeBytes", "url"}
    assert panel_artifacts["adjudication"] == {
        "fileName": "adjudication.json",
        "sizeBytes": 64,
        "url": "/media/scores/panel/s1/adjudication.json",
    }
    assert "absolutePath" not in str(public)


def test_public_session_single_model_scores_carry_null_panel_artifacts(tmp_path) -> None:
    service = _make_service(tmp_path)
    base_session = {
        "id": "s1",
        "name": "Case 1",
        "createdAt": "2026-01-01T00:00:00Z",
        "status": "completed",
        "pipeline": {},
        "files": {"video": None, "caseStudy": None},
        "outputs": {
            "audio": None,
            "videoClips": None,
            "scores": {
                "fileName": "s1.json",
                "absolutePath": "C:/secret/scores/s1.json",
                "url": "/media/scores/s1.json",
                "sizeBytes": 512,
            },
        },
        "error": None,
    }
    public = service.public_session(base_session)
    assert public["outputs"]["scores"] == {
        "fileName": "s1.json",
        "sizeBytes": 512,
        "url": "/media/scores/s1.json",
        "panelArtifacts": None,
    }

    missing_session = {**base_session, "outputs": {**base_session["outputs"], "scores": None}}
    public_missing = service.public_session(missing_session)
    assert public_missing["outputs"]["scores"] is None


def test_public_session_degraded_panel_has_markers_but_no_adjudication(tmp_path) -> None:
    service = _make_service(tmp_path)
    public = service.public_session(
        {
            "id": "s1",
            "name": "Case 1",
            "createdAt": "2026-01-01T00:00:00Z",
            "status": "completed",
            "pipeline": {},
            "files": {"video": None, "caseStudy": None},
            "outputs": {
                "audio": None,
                "videoClips": None,
                "scores": {
                    "fileName": "s1.json",
                    "absolutePath": "C:/secret/scores/s1.json",
                    "url": "/media/scores/s1.json",
                    "sizeBytes": 512,
                    "panelArtifacts": {
                        "markers": {
                            "nvidia__nemotron": {
                                "fileName": "nvidia__nemotron.json",
                                "absolutePath": "C:/secret/scores/panel/s1/nvidia__nemotron.json",
                                "url": "/media/scores/panel/s1/nvidia__nemotron.json",
                                "sizeBytes": 128,
                            },
                        },
                        "adjudication": None,
                    },
                },
            },
            "error": None,
        }
    )
    panel_artifacts = public["outputs"]["scores"]["panelArtifacts"]
    assert list(panel_artifacts["markers"]) == ["nvidia__nemotron"]
    assert panel_artifacts["adjudication"] is None
    assert "absolutePath" not in str(public)


def test_reserve_unique_session_name_handles_conflicts(tmp_path) -> None:
    service = _make_service(tmp_path)
    used = {"case"}
    assert service.reserve_unique_session_name(used, "Case") == "Case 2"
    assert "case 2" in used


def test_ensure_names_keeps_historical_name_and_suffixes_newer_duplicate(tmp_path) -> None:
    """On a name collision the historical session keeps its name; the newer
    duplicate gets a numeric suffix — never a random rename."""
    service = _make_service(tmp_path)

    async def scenario():
        await service.write(
            {"id": "old", "name": "Case", "createdAt": "2026-01-01T00:00:00Z", "status": "uploaded", "outputs": {}}
        )
        await service.write(
            {"id": "new", "name": "Case", "createdAt": "2026-02-01T00:00:00Z", "status": "uploaded", "outputs": {}}
        )
        entries, _used = await service.ensure_names_for_index(await service.read_all_entries())
        order = [str(entry.session["id"]) for entry in entries]
        names = {str(entry.session["id"]): entry.session["name"] for entry in entries}
        persisted = {sid: (await service.read(sid))["name"] for sid in ("old", "new")}
        return order, names, persisted

    order, names, persisted = asyncio.run(scenario())
    assert names["old"] == "Case"
    assert names["new"] == "Case 2"
    assert persisted == {"old": "Case", "new": "Case 2"}
    # Callers (list projection) still receive newest-first ordering.
    assert order == ["new", "old"]


def test_ensure_names_derives_meaningful_fallback_for_unnamed_sessions(tmp_path) -> None:
    """Unnamed (historical) sessions are named after their uploaded video file,
    falling back to the creation date — not a random adjective-noun pair."""
    service = _make_service(tmp_path)

    async def scenario():
        await service.write(
            {
                "id": "s-video",
                "name": None,
                "createdAt": "2026-03-01T10:30:00Z",
                "status": "uploaded",
                "files": {"video": {"originalName": "OSCE Station 3.mp4"}},
                "outputs": {},
            }
        )
        await service.write(
            {"id": "s-bare", "name": None, "createdAt": "2026-03-02T08:15:00Z", "status": "uploaded", "outputs": {}}
        )
        entries, _used = await service.ensure_names_for_index(await service.read_all_entries())
        return {str(entry.session["id"]): entry.session["name"] for entry in entries}

    names = asyncio.run(scenario())
    assert names["s-video"] == "OSCE Station 3"
    assert names["s-bare"] == "Session 2026-03-02 08:15"
