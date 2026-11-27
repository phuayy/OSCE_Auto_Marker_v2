"""prepare_media announces a finished conversion — and only that.

A copy that was not there before ("Video ready to watch") and a copy that could
not be made ("Video could not be converted") reach the bell and webhooks. An
upload the browser plays as it is, a re-run that adopts an existing copy, a
purged video and a cancelled run stay silent.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings
from app.core.resources import ResourceLease
from app.domain.enums import MediaPreparationStatus
from app.domain.notifications import SUBSCRIBABLE_EVENT_TYPES, NotificationType, event_types_match
from app.pipeline.media import MediaPipeline
from app.services.media_preparation import MediaPreparationOutcome, prepare_session_media
from app.services.pipeline_service import PipelineService
from tests.fixtures.session_store import SessionUpdateMixin


class _Runner:
    def __init__(self, codec: str) -> None:
        self.codec = codec
        self.fail_encode = False

    async def run(self, binary: str, args: list[str], label: str, **_: Any) -> Any:
        if "ffprobe" in label:
            return SimpleNamespace(stdout=json.dumps({
                "streams": [{"codec_type": "video", "codec_name": self.codec, "pix_fmt": "yuv420p"}],
                "format": {"duration": "100"},
            }))
        if "playback" in label and self.fail_encode:
            raise RuntimeError("ffmpeg died")
        Path(args[-1]).write_bytes(b"x")
        return SimpleNamespace(stdout="")


class _Sessions(SessionUpdateMixin):
    def __init__(self, session: dict[str, Any]) -> None:
        self.session = session

    async def read(self, _id: str) -> dict[str, Any]:
        return copy.deepcopy(self.session)

    async def write(self, session: dict[str, Any]) -> None:
        self.session = session


class _Notifications:
    def __init__(self, result: Any = None) -> None:
        self.emitted: list[tuple[Any, str, str, str | None]] = []
        self.result = result

    async def emit(self, event_type: Any, title: str, body: str, *, session_id: str | None = None) -> Any:
        self.emitted.append((event_type, title, body, session_id))
        return self.result


def _build(tmp_path: Path, codec: str = "hevc", *, playback: dict[str, Any] | None = None, name: str | None = "Station 4"):
    settings = Settings(root_dir=tmp_path, backend_root=tmp_path, media_progress_write_interval_seconds=0.0)
    runner = _Runner(codec)
    media = MediaPipeline(settings, runner, events=None, auth=None, gpu=ResourceLease.unbounded())  # type: ignore[arg-type]
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    video: dict[str, Any] = {"absolutePath": str(source)}
    if playback:
        video["playback"] = playback
    session: dict[str, Any] = {"id": "s1", "files": {"video": video}}
    if name:
        session["name"] = name
    return media, runner, session


def _run(media: Any, session: dict[str, Any], notifications: Any):
    service = PipelineService(_Sessions(session), events=None, media=media, scoring=None, notifications=notifications)
    asyncio.run(service.prepare_media_by_id("s1"))
    return service


def test_a_new_copy_raises_media_ready(tmp_path: Path) -> None:
    media, _, session = _build(tmp_path)
    notes = _Notifications()
    service = _run(media, session, notes)
    assert len(notes.emitted) == 1
    event, title, body, session_id = notes.emitted[0]
    assert event is NotificationType.MEDIA_READY and session_id == "s1"
    assert title == "Video ready to watch"
    assert body == '"Station 4": a playable copy of the recording is ready.'
    assert service.sessions.session["files"]["video"]["playback"]


def test_the_session_id_stands_in_for_a_missing_name(tmp_path: Path) -> None:
    media, _, session = _build(tmp_path, name=None)
    notes = _Notifications()
    _run(media, session, notes)
    assert notes.emitted[0][2].startswith('"s1":')


def test_a_failed_copy_raises_media_failed_once(tmp_path: Path) -> None:
    media, runner, session = _build(tmp_path)
    runner.fail_encode = True
    notes = _Notifications()
    service = _run(media, session, notes)
    assert [e[0] for e in notes.emitted] == [NotificationType.MEDIA_FAILED]
    _, title, body, session_id = notes.emitted[0]
    assert title == "Video could not be converted" and session_id == "s1"
    assert body == (
        '"Station 4": the server could not make a playable copy. '
        "The split and scoring are unaffected; open the session to try again."
    )
    assert service.sessions.session["files"]["video"]["media"]["status"] == MediaPreparationStatus.FAILED


def test_a_browser_playable_upload_is_silent(tmp_path: Path) -> None:
    media, _, session = _build(tmp_path, codec="h264")
    notes = _Notifications()
    _run(media, session, notes)
    assert notes.emitted == []


def test_an_already_recorded_copy_is_silent(tmp_path: Path) -> None:
    media, _, session = _build(tmp_path, playback={"url": "/media/playback/s1/s1-playback.mp4", "absolutePath": "x"})
    notes = _Notifications()
    _run(media, session, notes)
    assert notes.emitted == []


def test_a_purged_video_is_silent(tmp_path: Path) -> None:
    media, _, _ = _build(tmp_path)
    session = {"id": "s1", "files": {"video": {"absolutePath": None, "purgedAt": "2026-01-01T00:00:00Z"}}}
    notes = _Notifications()
    _run(media, session, notes)
    assert notes.emitted == []


@pytest.mark.parametrize(
    ("codec", "fail", "playback", "expected"),
    [
        ("hevc", False, None, MediaPreparationOutcome.COPY_WRITTEN),
        ("hevc", True, None, MediaPreparationOutcome.FAILED),
        ("h264", False, None, MediaPreparationOutcome.NOT_NEEDED),
        ("hevc", False, {"url": "u", "absolutePath": "x"}, MediaPreparationOutcome.NOT_NEEDED),
    ],
)
def test_outcome_is_returned_by_the_job_step(tmp_path: Path, codec: str, fail: bool, playback: Any, expected: Any) -> None:
    media, runner, session = _build(tmp_path, codec, playback=playback)
    runner.fail_encode = fail

    async def commit(current: dict[str, Any], mutate: Any) -> None:
        mutate(current)

    assert asyncio.run(prepare_session_media(media, session, commit)) is expected


def test_no_source_is_skipped(tmp_path: Path) -> None:
    media, _, _ = _build(tmp_path)
    session = {"id": "s1", "files": {}}

    async def commit(current: dict[str, Any], mutate: Any) -> None:
        mutate(current)

    assert asyncio.run(prepare_session_media(media, session, commit)) is MediaPreparationOutcome.SKIPPED


def test_a_cancelled_run_raises_records_failure_and_is_silent(tmp_path: Path) -> None:
    media, _, session = _build(tmp_path)

    async def hang(_session: dict[str, Any], **_: Any) -> None:
        await asyncio.sleep(3600)

    media.ensure_playback_copy = hang  # type: ignore[method-assign]
    notes = _Notifications()
    service = PipelineService(_Sessions(session), events=None, media=media, scoring=None, notifications=notes)

    async def go() -> None:
        task = asyncio.create_task(service.prepare_media_by_id("s1"))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if service.sessions.session["files"]["video"].get("media", {}).get("stage") == "poster":
                await asyncio.sleep(0.05)
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    media_record = service.sessions.session["files"]["video"]["media"]
    assert media_record["status"] == MediaPreparationStatus.FAILED
    assert media_record["error"] == "Conversion was cancelled."
    assert notes.emitted == []


def test_the_job_completes_without_a_notification_service(tmp_path: Path) -> None:
    media, _, session = _build(tmp_path)
    service = _run(media, session, None)
    assert service.sessions.session["files"]["video"]["media"]["status"] == MediaPreparationStatus.READY


def test_a_lost_notification_does_not_affect_the_job(tmp_path: Path) -> None:
    media, _, session = _build(tmp_path)
    notes = _Notifications(result=None)  # NotificationService.emit answers None when the row was lost
    service = _run(media, session, notes)
    assert len(notes.emitted) == 1
    assert service.sessions.session["files"]["video"]["media"]["status"] == MediaPreparationStatus.READY


def test_media_events_are_subscribable_and_match_the_wildcard() -> None:
    assert {"media.ready", "media.failed"} <= set(SUBSCRIBABLE_EVENT_TYPES)
    assert event_types_match(["*"], NotificationType.MEDIA_READY.value)
    assert event_types_match(["media.failed"], NotificationType.MEDIA_FAILED.value)
    assert not event_types_match(["media.failed"], NotificationType.MEDIA_READY.value)
