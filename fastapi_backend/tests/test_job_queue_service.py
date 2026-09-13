from __future__ import annotations

import asyncio

from app.core.config import Settings
from app.database.orm import OrmDatabase
from app.repositories.job_repository import JobRepository
from app.services.event_service import EventService
from app.services.job_queue_service import JobQueueService, compute_retry_backoff_seconds


def _make_service(tmp_path) -> JobQueueService:
    settings = Settings(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", scorer_python_bin="python")
    repository = JobRepository(OrmDatabase(tmp_path / "osce_marker.sqlite3"))
    events = EventService(settings)
    # sessions/storage are unused by the enqueue path under test.
    return JobQueueService(settings, repository, events, sessions=None, storage=None)


# --- L3: retry backoff ------------------------------------------------------

def test_first_attempt_has_no_backoff() -> None:
    assert compute_retry_backoff_seconds(1) == 0.0
    assert compute_retry_backoff_seconds(0) == 0.0


def test_retry_backoff_is_jittered_within_equal_jitter_bounds() -> None:
    # attempt 2 -> ceiling = base*2^0 = 1.0 -> [0.5, 1.0]
    for _ in range(100):
        value = compute_retry_backoff_seconds(2)
        assert 0.5 <= value <= 1.0
    # attempt 4 -> ceiling = base*2^2 = 4.0 -> [2.0, 4.0]
    for _ in range(100):
        value = compute_retry_backoff_seconds(4)
        assert 2.0 <= value <= 4.0


def test_retry_backoff_is_capped() -> None:
    # Large attempt is clamped to cap (30s) -> [15, 30].
    for _ in range(100):
        value = compute_retry_backoff_seconds(50)
        assert 15.0 <= value <= 30.0


# --- L2: enqueue dedup race -------------------------------------------------

def test_concurrent_enqueue_creates_single_active_job(tmp_path) -> None:
    service = _make_service(tmp_path)

    async def scenario() -> None:
        await service.repository.initialize()
        # Fire many concurrent enqueues for the same session+task.
        await asyncio.gather(
            *(
                service.enqueue("session-1", "process_session", {"workflow": "standard"}, auto_start=False)
                for _ in range(8)
            )
        )
        jobs = await service.repository.list_for_session("session-1")
        active = [j for j in jobs if str(j.get("status")) in {"queued", "running", "waiting_for_upload"}]
        assert len(active) == 1

    asyncio.run(scenario())


def test_concurrent_create_waiting_job_is_deduplicated(tmp_path) -> None:
    service = _make_service(tmp_path)

    async def scenario() -> None:
        await service.repository.initialize()
        await asyncio.gather(
            *(service.create_waiting_job("session-2", "auto_crop", {"workflow": "long"}) for _ in range(8))
        )
        jobs = await service.repository.list_for_session("session-2")
        assert len(jobs) == 1

    asyncio.run(scenario())
