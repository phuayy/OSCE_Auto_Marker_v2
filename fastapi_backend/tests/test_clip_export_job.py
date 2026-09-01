"""Clip export as a durable job.

Cutting N students out of a long recording used to run inline in
``POST /sessions/{id}/clips/manual``: N ffmpeg passes inside one HTTP request,
with no job row, no progress and nothing to resume from. These tests pin the
behaviour that replaced it — the request only plans, the job cuts, and the job
picks up where a previous attempt stopped.
"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.exceptions import AppError
from app.pipeline.media import MediaPipeline
from app.services.clip_service import ClipService
from app.services.job_tasks import get_task_spec, queue_owns_session_status


class FakeEvents:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict[str, Any]]] = []

    async def publish(self, session_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self.items.append((session_id, event_type, payload))


class FakeSessions:
    """Session store that records every write, so checkpointing is observable."""

    def __init__(self, initial_session: dict[str, Any]) -> None:
        self.current = copy.deepcopy(initial_session)
        self.writes: list[dict[str, Any]] = []

    async def read(self, session_id: str) -> dict[str, Any]:
        if str(self.current.get("id")) != str(session_id):
            raise FileNotFoundError(f"Session not found: {session_id}")
        return copy.deepcopy(self.current)

    async def write(self, session: dict[str, Any]) -> None:
        self.current = copy.deepcopy(session)
        self.writes.append(copy.deepcopy(session))

    def public_session(self, session: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(session)


class FakeJobRepository:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def read(self, job_id: str) -> dict[str, Any]:
        return self.rows[job_id]


class FakeJobs:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, str, dict[str, Any]]] = []
        self.repository = FakeJobRepository()

    async def enqueue(self, session_id: str, task_type: str, payload: dict[str, Any] | None = None):
        self.enqueued.append((session_id, task_type, payload or {}))
        job = {
            "id": f"job-{len(self.enqueued)}",
            "sessionId": session_id,
            "taskType": task_type,
            "status": "queued",
        }
        self.repository.rows[job["id"]] = job
        return job

    def public_job(self, job: dict[str, Any] | None) -> dict[str, Any] | None:
        return dict(job) if job else None


class RecordingMedia(MediaPipeline):
    """Real clip planning and file-reuse logic; ffmpeg replaced by a stub cut."""

    def __init__(self, settings: Settings, *, fail_on_index: int | None = None) -> None:
        self.settings = settings
        self.crops: list[Path] = []
        self.fail_on_index = fail_on_index

    async def get_video_duration_seconds(self, _path: Path) -> float:
        return 600.0

    async def crop_video_segment(self, *, input_path, start_seconds, end_seconds, output_path) -> bool:
        _ = (input_path, start_seconds, end_seconds)
        if self.fail_on_index is not None and len(self.crops) == self.fail_on_index:
            raise RuntimeError("ffmpeg exploded")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"clip-bytes")
        self.crops.append(output_path)
        return True


class FakePipeline:
    @staticmethod
    def now_iso() -> str:
        return "2026-01-01T00:00:00Z"


def _build(tmp_path, *, fail_on_index: int | None = None):
    settings = Settings(root_dir=tmp_path, backend_root=tmp_path, auto_crop_min_clip_seconds=1.0)
    video_path = tmp_path / "recording.mp4"
    video_path.write_bytes(b"video")
    session = {
        "id": "sess-1",
        "name": "Long Station",
        "status": "uploaded",
        "workflow": "long",
        "files": {"video": {"absolutePath": str(video_path)}},
        "outputs": {},
    }
    sessions = FakeSessions(session)
    events = FakeEvents()
    media = RecordingMedia(settings, fail_on_index=fail_on_index)
    jobs = FakeJobs()
    service = ClipService(sessions, events, media, FakePipeline(), jobs=jobs)
    return service, sessions, events, media, jobs


def test_request_only_plans_and_queues_no_cropping_in_the_request(tmp_path) -> None:
    """The POST must not run ffmpeg — that is the whole point of the change."""
    service, sessions, _events, media, jobs = _build(tmp_path)

    result = asyncio.run(service.request_clip_export("sess-1", [200.0, 400.0], [], None))

    assert media.crops == [], "the request cut clips inline"
    assert jobs.enqueued == [("sess-1", "export_clips", {"clipCount": 3})]
    assert result["clipCount"] == 3
    export = sessions.current["clipExport"]
    assert export["status"] == "queued"
    assert (export["total"], export["completed"]) == (3, 0)
    assert export["jobId"] == "job-1"
    # Drafts are persisted immediately so the timeline can render the split.
    clips = sessions.current["outputs"]["videoClips"]
    assert [clip["exportIndex"] for clip in clips] == [0, 1, 2]
    assert all(clip["isDraft"] for clip in clips)


def test_export_job_cuts_every_clip_and_checkpoints_after_each(tmp_path) -> None:
    service, sessions, _events, media, _jobs = _build(tmp_path)
    asyncio.run(service.request_clip_export("sess-1", [200.0, 400.0], [], None))

    result = asyncio.run(service.export_clips_by_id("sess-1"))

    assert len(media.crops) == 3
    assert result["clipCount"] == 3
    assert sessions.current["clipExport"]["status"] == "completed"
    assert sessions.current["status"] == "cropped"
    assert all(not clip["isDraft"] for clip in sessions.current["outputs"]["videoClips"])
    # One durable checkpoint per finished clip: that is what a resumed run reads.
    counters = [
        write["clipExport"]["completed"]
        for write in sessions.writes
        if write.get("clipExport", {}).get("status") == "running"
    ]
    assert counters == [0, 1, 2, 3]


def test_a_retried_export_reuses_clips_that_already_finished(tmp_path) -> None:
    """The resume guarantee: a retry costs only the work that never completed."""
    service, sessions, _events, media, _jobs = _build(tmp_path, fail_on_index=2)
    asyncio.run(service.request_clip_export("sess-1", [200.0, 400.0], [], None))

    try:
        asyncio.run(service.export_clips_by_id("sess-1"))
    except RuntimeError:
        pass
    assert len(media.crops) == 2
    assert sessions.current["clipExport"]["status"] == "failed"
    # A failed export must not bury the session: its clip list is still valid.
    assert sessions.current["status"] == "uploaded"

    media.fail_on_index = None
    asyncio.run(service.export_clips_by_id("sess-1"))

    # Only the third clip is cut on the retry; the first two are adopted.
    assert len(media.crops) == 3
    assert sessions.current["clipExport"]["status"] == "completed"
    assert sessions.current["clipExport"]["completed"] == 3


def test_a_second_export_is_refused_while_one_is_in_flight(tmp_path) -> None:
    service, _sessions, _events, _media, _jobs = _build(tmp_path)
    asyncio.run(service.request_clip_export("sess-1", [200.0], [], None))

    try:
        asyncio.run(service.request_clip_export("sess-1", [300.0], [], None))
    except AppError as error:
        assert error.status_code == 409
    else:  # pragma: no cover - the guard regressed
        raise AssertionError("a concurrent export request was accepted")


def test_intermissions_are_planned_but_never_cut(tmp_path) -> None:
    service, sessions, _events, media, jobs = _build(tmp_path)

    asyncio.run(
        service.request_clip_export("sess-1", [200.0, 400.0], [], ["session", "intermission", "session"])
    )
    asyncio.run(service.export_clips_by_id("sess-1"))

    assert jobs.enqueued[0][2] == {"clipCount": 2}
    assert len(media.crops) == 2
    clips = sessions.current["outputs"]["videoClips"]
    intermission = next(clip for clip in clips if clip["kind"] == "intermission")
    assert intermission["fileName"] is None


def test_the_queue_does_not_own_the_session_status_for_an_export(tmp_path) -> None:
    """Why it matters: the user sits in the timeline editor while clips are cut.

    ``process_session``/``auto_crop`` flip the session to "processing", which
    the frontend refuses to open. An export doing the same would eject the user
    from the session they are working in.
    """
    _ = tmp_path
    assert queue_owns_session_status("process_session") is True
    assert queue_owns_session_status("auto_crop") is True
    assert queue_owns_session_status("export_clips") is False
    # An unknown type (a job row written by an older build) keeps the old
    # behaviour rather than going silently unreported.
    assert queue_owns_session_status("something_new") is True
    assert get_task_spec("export_clips").name == "export_clips"


def test_an_unknown_task_type_is_rejected_by_the_registry() -> None:
    try:
        get_task_spec("not_a_task")
    except AppError as error:
        assert error.status_code == 500
    else:  # pragma: no cover - the registry regressed
        raise AssertionError("an unknown task type was accepted")


def test_an_export_whose_job_already_died_does_not_lock_the_session(tmp_path) -> None:
    """A killed worker must not leave a 409 the user can never clear.

    The session's own clipExport record still reads "running" after a hard kill.
    The job row is the authority on whether anything is actually in flight.
    """
    service, sessions, _events, _media, jobs = _build(tmp_path)
    asyncio.run(service.request_clip_export("sess-1", [200.0], [], None))
    sessions.current["clipExport"]["status"] = "running"
    jobs.repository.rows["job-1"]["status"] = "failed"

    asyncio.run(service.request_clip_export("sess-1", [300.0], [], None))

    assert len(jobs.enqueued) == 2
    assert sessions.current["clipExport"]["status"] == "queued"
