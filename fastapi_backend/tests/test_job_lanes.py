"""The media lane: a prepare_media job never waits behind (or takes) a main slot."""
from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import Settings
from app.database.orm import OrmDatabase
from app.domain.enums import JobLane, TaskType
from app.repositories.job_repository import JobRepository
from app.services.event_service import EventService
from app.services.job_queue_service import JobQueueService
from app.services.job_tasks import task_lane
from tests.fixtures.session_store import SessionUpdateMixin


class FakeSessions(SessionUpdateMixin):
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}

    async def read(self, session_id: str) -> dict[str, Any]:
        return dict(self.sessions.setdefault(session_id, {"id": session_id, "status": "queued"}))

    async def write(self, session: dict[str, Any]) -> None:
        self.sessions[str(session["id"])] = dict(session)


class FakeStorage:
    async def prepare_session_sources(self, session: dict[str, Any]) -> dict[str, Any]:
        return session


class GatedPipeline:
    """Handlers that park on an event, recording what runs at once."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.running: list[str] = []
        self.peak: dict[str, int] = {"main": 0, "media": 0}
        self.started: dict[str, asyncio.Event] = {}

    def started_event(self, session_id: str) -> asyncio.Event:
        return self.started.setdefault(session_id, asyncio.Event())

    async def _park(self, lane: str, session_id: str) -> None:
        self.running.append(session_id)
        self.peak[lane] = max(self.peak[lane], sum(1 for s in self.running if s.startswith(lane)))
        self.started_event(session_id).set()
        await self.release.wait()
        self.running.remove(session_id)

    async def process_session_by_id(self, session_id: str, *, allow_processing: bool = False) -> None:
        await self._park("main", session_id)

    async def prepare_media_by_id(self, session_id: str) -> None:
        await self._park("media", session_id)

    async def mark_session_failed(self, *_: Any) -> None:
        return None


def make_service(tmp_path, pipeline: GatedPipeline, **overrides: Any) -> JobQueueService:
    settings = Settings(
        ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", scorer_python_bin="python",
        job_queue_backend="local", local_job_auto_start=True, **overrides,
    )
    service = JobQueueService(
        settings, JobRepository(OrmDatabase(tmp_path / "q.sqlite3")), EventService(settings), FakeSessions(), FakeStorage()
    )
    service.bind_handlers(pipeline=pipeline, clips=object())
    return service


async def wait_for(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), 5)


def test_media_job_runs_while_both_main_slots_are_busy(tmp_path) -> None:
    pipeline = GatedPipeline()
    service = make_service(tmp_path, pipeline, job_worker_concurrency=2)

    async def go() -> None:
        await service.repository.initialize()
        for name in ("main-1", "main-2", "main-3"):
            await service.enqueue(name, TaskType.PROCESS_SESSION, {}, auto_start=True)
        await wait_for(pipeline.started_event("main-1"))
        await wait_for(pipeline.started_event("main-2"))
        assert not pipeline.started_event("main-3").is_set()  # the third main job waits
        await service.enqueue("media-1", TaskType.PREPARE_MEDIA, {}, auto_start=True)
        await wait_for(pipeline.started_event("media-1"))  # but media does not
        pipeline.release.set()
        await asyncio.gather(*(t for t in list(service._tasks.values())))

    asyncio.run(go())
    assert pipeline.peak["main"] == 2


def test_media_concurrency_one_serialises_media_jobs(tmp_path) -> None:
    pipeline = GatedPipeline()
    service = make_service(tmp_path, pipeline, media_job_concurrency=1)

    async def go() -> None:
        await service.repository.initialize()
        await service.enqueue("media-a", TaskType.PREPARE_MEDIA, {}, auto_start=True)
        await service.enqueue("media-b", TaskType.PREPARE_MEDIA, {}, auto_start=True)
        await wait_for(pipeline.started_event("media-a"))
        await asyncio.sleep(0.1)
        assert not pipeline.started_event("media-b").is_set()
        pipeline.release.set()
        await asyncio.gather(*(t for t in list(service._tasks.values())))
        assert pipeline.started_event("media-b").is_set()

    asyncio.run(go())
    assert pipeline.peak["media"] == 1


def test_invalid_concurrency_values_clamp_to_one(tmp_path) -> None:
    service = make_service(tmp_path, GatedPipeline(), media_job_concurrency=0, job_worker_concurrency=-3)

    async def go() -> None:
        for lane in JobLane:
            semaphore = service._lane_semaphore(lane)
            await asyncio.wait_for(semaphore.acquire(), 1)
            assert semaphore.locked()

    asyncio.run(go())


def test_task_lanes() -> None:
    assert task_lane(TaskType.PREPARE_MEDIA) == JobLane.MEDIA
    for task in (TaskType.PROCESS_SESSION, TaskType.AUTO_CROP, TaskType.EXPORT_CLIPS, "unknown-from-older-build"):
        assert task_lane(task) == JobLane.MAIN


# --- an abandoned media preparation does not stay "running" --------------------

from app.domain.enums import MediaPreparationStatus  # noqa: E402


def session_with_media(status: str | None, *, purged: bool = False) -> dict[str, Any]:
    video: dict[str, Any] = {"absolutePath": None if purged else "/x.mp4", "purgedAt": "2026-01-01" if purged else None}
    if status is not None:
        video["media"] = {"status": status, "stage": "converting", "progress": 0.4}
    return {"id": "s", "status": "completed", "files": {"video": video}}


class ProjectingSessions(FakeSessions):
    """The fake store, with the list projection's media block."""

    async def list_sessions(self) -> list[dict[str, Any]]:
        rows = []
        for session in self.sessions.values():
            video = (session.get("files") or {}).get("video") or {}
            media = None if video.get("purgedAt") else video.get("media")
            rows.append({"id": session["id"], "status": session["status"], "media": media})
        return rows


def make_reconciling(tmp_path) -> JobQueueService:
    service = make_service(tmp_path, GatedPipeline())
    service.sessions = ProjectingSessions()
    return service


def media_of(service: JobQueueService, session_id: str = "s") -> dict[str, Any]:
    return service.sessions.sessions[session_id]["files"]["video"]["media"]  # type: ignore[attr-defined]


def test_orphaned_pending_media_is_failed_at_startup(tmp_path) -> None:
    service = make_reconciling(tmp_path)

    async def go() -> list[str]:
        await service.repository.initialize()
        for status in (MediaPreparationStatus.QUEUED, MediaPreparationStatus.RUNNING):
            service.sessions.sessions[f"s-{status}"] = {**session_with_media(status), "id": f"s-{status}"}  # type: ignore[attr-defined]
        return await service.reconcile_orphaned_media()

    assert sorted(asyncio.run(go())) == ["s-queued", "s-running"]
    for key in ("s-queued", "s-running"):
        media = media_of(service, key)
        assert media["status"] == MediaPreparationStatus.FAILED
        assert media["error"] == "Interrupted by a server restart."
        assert media["stage"] is None and media["progress"] is None


def test_media_with_an_active_job_ready_purged_or_absent_is_untouched(tmp_path) -> None:
    service = make_reconciling(tmp_path)

    async def go() -> list[str]:
        await service.repository.initialize()
        store = service.sessions.sessions  # type: ignore[attr-defined]
        store["s"] = session_with_media(MediaPreparationStatus.RUNNING)
        await service.enqueue("s", TaskType.PREPARE_MEDIA, {}, auto_start=False)
        store["ready"] = {**session_with_media(MediaPreparationStatus.READY), "id": "ready"}
        store["purged"] = {**session_with_media(MediaPreparationStatus.RUNNING, purged=True), "id": "purged"}
        store["none"] = {**session_with_media(None), "id": "none"}
        # An active job of ANOTHER type does not vouch for the preparation.
        store["other"] = {**session_with_media(MediaPreparationStatus.RUNNING), "id": "other"}
        await service.enqueue("other", TaskType.AUTO_CROP, {}, auto_start=False)
        return await service.reconcile_orphaned_media()

    assert asyncio.run(go()) == ["other"]
    assert media_of(service)["status"] == MediaPreparationStatus.RUNNING
    assert media_of(service, "ready")["status"] == MediaPreparationStatus.READY
    assert media_of(service, "purged")["status"] == MediaPreparationStatus.RUNNING


def test_exhausted_media_job_ends_the_media_record_failed(tmp_path) -> None:
    service = make_reconciling(tmp_path)

    async def go() -> None:
        await service.repository.initialize()
        service.sessions.sessions["s"] = session_with_media(MediaPreparationStatus.QUEUED)  # type: ignore[attr-defined]
        job = await service.enqueue("s", TaskType.PREPARE_MEDIA, {}, auto_start=False)
        # Recovered at boot with every attempt already spent.
        async with service.repository.database.session() as db:
            from app.database.models import JobRecord

            record = await db.get(JobRecord, job["id"])
            record.attempts = record.max_attempts
            await db.commit()
        result = await service.run_job(str(job["id"]), lane=JobLane.MEDIA)
        assert result.error is not None

    asyncio.run(go())
    media = media_of(service)
    assert media["status"] == MediaPreparationStatus.FAILED and media["progress"] is None and media["stage"] is None


def test_startup_reconciles_media_only_in_the_process_that_owns_recovery(tmp_path) -> None:
    service = make_reconciling(tmp_path)

    async def go() -> None:
        service.sessions.sessions["s"] = session_with_media(MediaPreparationStatus.RUNNING)  # type: ignore[attr-defined]
        await service.startup(dispatch_queued=False, recover_interrupted=False)  # a Hatchet worker
        assert media_of(service)["status"] == MediaPreparationStatus.RUNNING
        await service.startup(dispatch_queued=False)  # the API
        assert media_of(service)["status"] == MediaPreparationStatus.FAILED

    asyncio.run(go())
