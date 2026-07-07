"""Wiring tests for the auto-crop segmentation method (bells vs person/RT-DETR).

Covers the full choice path: upload schema -> session persistence ->
ClipService detector selection -> graceful fallback to bell detection when the
person detector fails.
"""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.services.clip_service import ClipService

from .test_routes import build_test_client


class FakeEvents:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict[str, Any]]] = []

    async def publish(self, session_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self.items.append((session_id, event_type, payload))


class FakeSessions:
    def __init__(self, initial_session: dict[str, Any]) -> None:
        self.current = copy.deepcopy(initial_session)

    async def read(self, session_id: str) -> dict[str, Any]:
        if str(self.current.get("id")) != str(session_id):
            raise FileNotFoundError(f"Session not found: {session_id}")
        return copy.deepcopy(self.current)

    async def write(self, session: dict[str, Any]) -> None:
        self.current = copy.deepcopy(session)

    def public_session(self, session: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(session)


class FakeMedia:
    """Records which detector ran; configurable person-detector failure."""

    def __init__(self, settings: Settings, *, person_error: Exception | None = None) -> None:
        self.settings = settings
        self.person_error = person_error
        self.person_calls: list[dict[str, Any]] = []
        self.bell_calls: list[dict[str, Any]] = []

    async def get_video_duration_seconds(self, _path: Path) -> float:
        return 120.0

    async def detect_person_clip_ranges_with_python(
        self,
        source_path: Path,
        video_duration_seconds: float,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self.person_calls.append({"path": source_path, "sessionId": session_id})
        if self.person_error is not None:
            raise self.person_error
        return {
            "clipRanges": [{"start": 0.0, "end": 60.0}, {"start": 70.0, "end": 120.0}],
            "source": {"type": "person_detection_rtdetr", "usedTrigger": "person_presence"},
        }

    async def detect_bell_clip_ranges_with_python(
        self,
        source_path: Path,
        video_duration_seconds: float,
        *,
        sample_rate: int,
    ) -> dict[str, Any]:
        self.bell_calls.append({"path": source_path, "sampleRate": sample_rate})
        return {
            "clipRanges": [{"start": 0.0, "end": 120.0}],
            "source": {"type": "bell_detection_python_librosa", "usedTrigger": "hybrid"},
        }

    def build_clip_drafts_from_ranges(
        self,
        clip_ranges: list[dict[str, float]],
        _video_duration_seconds: float,
        source_meta: dict[str, Any],
        label_overrides: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        _ = label_overrides
        return [
            {"id": f"clip-{index}", "start": item["start"], "end": item["end"], "source": source_meta}
            for index, item in enumerate(clip_ranges, start=1)
        ]


def build_settings(tmp_path: Path) -> Settings:
    return Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        app_database_url="",
        database_url="",
    )


def build_long_session(tmp_path: Path, segmentation: str | None) -> dict[str, Any]:
    video_path = tmp_path / "long.mp4"
    video_path.write_bytes(b"video")
    return {
        "id": "session-long-1",
        "name": "Long Session",
        "status": "uploaded",
        "workflow": "long",
        "segmentation": segmentation,
        "files": {"video": {"absolutePath": str(video_path)}},
        "outputs": {},
        "error": None,
    }


def build_clip_service(tmp_path: Path, segmentation: str | None, *, person_error: Exception | None = None):
    settings = build_settings(tmp_path)
    media = FakeMedia(settings, person_error=person_error)
    sessions = FakeSessions(build_long_session(tmp_path, segmentation))
    events = FakeEvents()
    service = ClipService(sessions=sessions, events=events, media=media, pipeline=None, jobs=None)
    return service, media, sessions, events


def test_auto_crop_uses_person_detector_when_selected(tmp_path) -> None:
    service, media, sessions, _events = build_clip_service(tmp_path, "person")
    result = asyncio.run(service.auto_crop_session_by_id("session-long-1"))

    assert len(media.person_calls) == 1
    assert media.person_calls[0]["sessionId"] == "session-long-1"
    assert media.bell_calls == []
    assert result["source"]["type"] == "person_detection_rtdetr"
    assert result["clipCount"] == 2
    assert sessions.current["status"] == "cropped"
    assert len(sessions.current["outputs"]["videoClips"]) == 2


def test_auto_crop_defaults_to_bell_detector(tmp_path) -> None:
    service, media, sessions, _events = build_clip_service(tmp_path, None)
    result = asyncio.run(service.auto_crop_session_by_id("session-long-1"))

    assert media.person_calls == []
    assert len(media.bell_calls) == 1
    assert result["source"]["type"] == "bell_detection_python_librosa"
    assert sessions.current["status"] == "cropped"


def test_auto_crop_ignores_unknown_segmentation_value(tmp_path) -> None:
    service, media, _sessions, _events = build_clip_service(tmp_path, "laser-eyes")
    asyncio.run(service.auto_crop_session_by_id("session-long-1"))

    assert media.person_calls == []
    assert len(media.bell_calls) == 1


def test_auto_crop_person_failure_falls_back_to_bells(tmp_path) -> None:
    service, media, sessions, events = build_clip_service(
        tmp_path,
        "person",
        person_error=RuntimeError("model weights unavailable"),
    )
    result = asyncio.run(service.auto_crop_session_by_id("session-long-1"))

    assert len(media.person_calls) == 1
    assert len(media.bell_calls) == 1
    assert result["source"]["type"] == "bell_detection_python_librosa"
    assert "person_detection_failed" in str(result["source"].get("fallbackFrom") or "")
    assert sessions.current["status"] == "cropped"
    # The fallback is surfaced in the live log, not silent.
    log_messages = [payload.get("message", "") for _sid, kind, payload in events.items if kind == "log"]
    assert any("Falling back to bell detection" in message for message in log_messages)


def test_initiate_persists_segmentation_choice(tmp_path) -> None:
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    response = client.post(
        "/api/uploads/initiate",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "workflow": "long",
            "autoProcess": True,
            "segmentation": "human",  # synonym normalises to "person"
            "files": [
                {"kind": "video", "originalName": "station.mp4", "mimeType": "video/mp4", "sizeBytes": 5},
                {"kind": "caseStudy", "originalName": "case.pdf", "mimeType": "application/pdf", "sizeBytes": 4},
            ],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["session"]["segmentation"] == "person"

    stored = asyncio.run(client.app.state.container.sessions.read(body["session"]["id"]))
    assert stored["segmentation"] == "person"


def test_initiate_standard_workflow_drops_segmentation(tmp_path) -> None:
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    response = client.post(
        "/api/uploads/initiate",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "workflow": "standard",
            "autoProcess": True,
            "segmentation": "person",
            "files": [
                {"kind": "video", "originalName": "station.mp4", "mimeType": "video/mp4", "sizeBytes": 5},
                {"kind": "caseStudy", "originalName": "case.pdf", "mimeType": "application/pdf", "sizeBytes": 4},
            ],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["session"]["segmentation"] is None


def test_initiate_rejects_invalid_segmentation(tmp_path) -> None:
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    response = client.post(
        "/api/uploads/initiate",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "workflow": "long",
            "autoProcess": True,
            "segmentation": "sonar",
            "files": [
                {"kind": "video", "originalName": "station.mp4", "mimeType": "video/mp4", "sizeBytes": 5},
                {"kind": "caseStudy", "originalName": "case.pdf", "mimeType": "application/pdf", "sizeBytes": 4},
            ],
        },
    )
    # Production main.py maps RequestValidationError to 400; the bare test app
    # uses FastAPI's default 422. Either way the invalid value must be rejected.
    assert response.status_code in {400, 422}
