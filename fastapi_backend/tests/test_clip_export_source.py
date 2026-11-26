"""Clips of a camera format no browser decodes are cut from the playback copy.

One transcode per recording: the H.264 copy ``prepare_media`` writes is also
what an export stream-copies its clips from, instead of re-encoding every clip
from the full-size original. And the notifications say what is true: the
auto-crop proposes boundaries (``split.ready``); only a finished export means
clips exist (``clips.ready``).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.core.config import Settings
from app.core.resources import ResourceLease
from app.domain.notifications import SUBSCRIBABLE_EVENT_TYPES, NotificationType
from app.pipeline.media import MediaPipeline
from app.services.clip_service import ClipService
from tests.fixtures.events import RecordingEvents
from tests.test_clip_export_job import FakeJobs, FakePipeline, FakeSessions
from tests.test_segmentation_wiring import build_clip_service


class PathAwareRunner:
    """ffprobe answers per file: the original is HEVC, anything under
    ``playback`` is the H.264 copy."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    async def run(self, binary: str, args: list[str], label: str, **_: Any) -> Any:
        self.calls.append((label, list(args)))
        if "ffprobe" in label:
            copy = "playback" in args[-1]
            stream = {"codec_type": "video", "codec_name": "h264" if copy else "hevc", "pix_fmt": "yuv420p"}
            return SimpleNamespace(stdout=json.dumps({"streams": [stream]}))
        Path(args[-1]).write_bytes(b"mp4")
        return SimpleNamespace(stdout="")


class RecordingNotifications:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, str, str]] = []

    async def emit(self, event_type: Any, title: str, body: str, **_: Any) -> None:
        self.emitted.append((str(getattr(event_type, "value", event_type)), title, body))


def _build(tmp_path: Path):
    settings = Settings(root_dir=tmp_path, backend_root=tmp_path, auto_crop_min_clip_seconds=1.0)
    video = tmp_path / "recording.mp4"
    video.write_bytes(b"video")
    session = {
        "id": "sess-1",
        "name": "Long Station",
        "status": "uploaded",
        "workflow": "long",
        "files": {"video": {"absolutePath": str(video)}},
        "outputs": {},
    }
    sessions = FakeSessions(session)
    runner = PathAwareRunner()
    media = MediaPipeline(settings, runner, None, None, gpu=ResourceLease.unbounded())  # type: ignore[arg-type]

    async def duration(_path: Path) -> float:
        return 600.0

    media.get_video_duration_seconds = duration  # type: ignore[method-assign]
    notifications = RecordingNotifications()
    service = ClipService(
        sessions, RecordingEvents(), media, FakePipeline(), jobs=FakeJobs(), notifications=notifications
    )
    return service, sessions, media, runner, notifications


def _phases(sessions: FakeSessions) -> list[Any]:
    seen: list[Any] = []
    for write in sessions.writes:
        phase = (write.get("clipExport") or {}).get("phase")
        if not seen or seen[-1] != phase:
            seen.append(phase)
    return seen


def test_hevc_export_cuts_clips_from_the_playback_copy_by_stream_copy(tmp_path: Path) -> None:
    service, sessions, _media, runner, notifications = _build(tmp_path)
    asyncio.run(service.request_clip_export("sess-1", [200.0, 400.0], [], None))
    asyncio.run(service.export_clips_by_id("sess-1"))

    crop_labels = [label for label, _ in runner.calls if "crop" in label]
    assert crop_labels == ["Video clip crop (stream copy)"] * 3
    crop_inputs = {args[args.index("-i") + 1] for label, args in runner.calls if "crop" in label}
    assert len(crop_inputs) == 1 and "playback" in next(iter(crop_inputs))
    # Exactly one transcode of the recording, however many clips.
    assert len([1 for label, _ in runner.calls if "playback copy" in label]) == 1

    video = sessions.current["files"]["video"]
    assert video["playback"]["url"] == "/media/playback/sess-1/sess-1-playback.mp4"
    assert [p for p in _phases(sessions) if p] == ["preparing_source", "cutting"]
    assert sessions.current["clipExport"]["phase"] is None
    assert [e[0] for e in notifications.emitted] == ["clips.ready"]


def test_export_falls_back_to_the_original_when_the_copy_cannot_be_made(tmp_path: Path) -> None:
    service, sessions, media, runner, _notifications = _build(tmp_path)

    async def broken(_session: dict[str, Any]) -> None:
        raise RuntimeError("disk full")

    media.ensure_playback_copy = broken  # type: ignore[method-assign]
    asyncio.run(service.request_clip_export("sess-1", [300.0], [], None))
    asyncio.run(service.export_clips_by_id("sess-1"))

    assert sessions.current["clipExport"]["status"] == "completed"
    assert "playback" not in sessions.current["files"]["video"]
    crop_labels = [label for label, _ in runner.calls if "crop" in label]
    assert crop_labels == ["Video clip crop (re-encode)"] * 2


def test_auto_crop_announces_a_split_to_review_not_ready_clips(tmp_path: Path) -> None:
    service, _media, _sessions, _events = build_clip_service(tmp_path, "bells")
    notifications = RecordingNotifications()
    service.notifications = notifications
    result = asyncio.run(service.auto_crop_session_by_id("session-long-1"))

    assert len(notifications.emitted) == 1
    event, title, body = notifications.emitted[0]
    assert event == NotificationType.SPLIT_READY.value == "split.ready"
    assert title == "Split ready for review"
    assert f"{result['clipCount']} clip" in body and "export them" in body
    assert "ready for assessment" not in body


def test_split_ready_is_subscribable() -> None:
    assert "split.ready" in SUBSCRIBABLE_EVENT_TYPES
