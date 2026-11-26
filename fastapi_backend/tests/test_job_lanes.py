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
