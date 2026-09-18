"""The stale-job reaper (A7).

``_execute_job``'s ``claim_queued`` call only ever caught ``FileNotFoundError``;
a DB error there, or inside ``mark_failed`` itself, propagated out of the
``asyncio.Task`` unretrieved and left the row claimed as ``running`` forever —
``recover_interrupted_jobs`` only runs at API startup on the ``local`` backend,
and nothing revisited a Hatchet-backend job stuck this way at all. These tests
exercise the fix directly: a heartbeat that proves a job is genuinely still
running, and a periodic reaper that only acts once that heartbeat goes stale.
"""

from __future__ import annotations

import asyncio
from typing import Any

from tests.test_job_queue_local_retry import ScriptedPipeline, make_service


class SlowPipeline:
    """Takes real wall-clock time per attempt, so a heartbeat has something to
    keep refreshing while it runs."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.attempts = 0
        self.failed_sessions: list[str] = []

    async def process_session_by_id(self, _session_id: str, *, allow_processing: bool = False) -> dict[str, Any]:
        self.attempts += 1
        await asyncio.sleep(self.seconds)
        return {"ok": True}

    async def mark_session_failed(self, session_id: str, _error: Exception) -> None:
        self.failed_sessions.append(session_id)


def test_heartbeat_keeps_a_slow_but_alive_job_from_being_reaped(tmp_path) -> None:
    """The false-positive this design exists to avoid: a legitimately long
    step (WhisperX, an LLM call under retry) must never be requeued out from
    under itself just for taking a while."""
    pipeline = SlowPipeline(seconds=0.6)
    service = make_service(
        tmp_path,
        pipeline,
        job_heartbeat_interval_seconds=0.05,
        job_stale_running_timeout_seconds=0.3,
    )

    async def scenario():
        await service.repository.initialize()
        job = await service.enqueue("session-slow", "process_session", {}, auto_start=True)
        job_id = str(job["id"])
        task = service._tasks.get(job_id)

        # Well past job_stale_running_timeout_seconds, but the heartbeat has
        # had several ticks to keep locked_at fresh by now.
        await asyncio.sleep(0.35)
        mid_flight = await service.reap_stale_jobs()

        if task is not None:
            await task
        final = await service.repository.read(job_id)
        return mid_flight, final

    mid_flight, final = asyncio.run(scenario())

    assert mid_flight == [], "a live heartbeat must never be reaped mid-run"
    assert final["status"] == "succeeded"
    assert pipeline.attempts == 1, "the job must not have been requeued and re-run"


def test_reap_stale_jobs_requeues_and_locally_resumes_an_orphaned_job(tmp_path, monkeypatch) -> None:
    """The bug itself: a job claimed by a task that then died with no
    heartbeat ever ticking again. list_stale_running is what finds it (see
    test_job_repository.py); this proves the service-level consequence —
    requeue, and (local backend) resume automatically."""
    monkeypatch.setattr("app.services.job_queue_service.compute_retry_backoff_seconds", lambda *_a, **_k: 0.0)
    pipeline = ScriptedPipeline(failures=0)
    service = make_service(tmp_path, pipeline, job_stale_running_timeout_seconds=0)

    async def scenario():
        await service.repository.initialize()
        # A job claimed and then abandoned: no live asyncio task, locked_at
        # frozen at claim time. job_stale_running_timeout_seconds=0 makes
        # "just claimed" already stale, so no real waiting is needed here —
        # the cutoff boundary itself is covered at the repository level.
        job = {
            "id": "job-orphaned",
            "sessionId": "session-orphaned",
            "taskType": "process_session",
            "status": "running",
            "attempts": 1,
            "maxAttempts": 3,
            "createdAt": "2026-01-01T00:00:00Z",
            "queuedAt": "2026-01-01T00:00:01Z",
            "startedAt": "2026-01-01T00:00:02Z",
            "endedAt": None,
            "error": None,
            "lockedBy": "dead-worker",
            "lockedAt": "2026-01-01T00:00:02Z",
            "payload": {},
        }
        await service.repository.write(job)

        reaped = await service.reap_stale_jobs()

        # Local auto-start dispatches synchronously inside reap_stale_jobs,
        # so the resumed run's task is already tracked.
        task = service._tasks.get("job-orphaned")
        if task is not None:
            await task
        final = await service.repository.read("job-orphaned")
        return reaped, final

    reaped, final = asyncio.run(scenario())

    assert reaped == ["job-orphaned"]
    assert final["status"] == "succeeded"
    assert pipeline.attempts == 1


def test_reap_stale_jobs_ignores_a_fresh_claim(tmp_path) -> None:
    service = make_service(tmp_path, ScriptedPipeline(failures=0), job_stale_running_timeout_seconds=3600)

    async def scenario():
        await service.repository.initialize()
        job = await service.enqueue("session-fresh", "process_session", {}, auto_start=False)
        await service.repository.claim_queued(str(job["id"]), "worker-1")
        return await service.reap_stale_jobs()

    assert asyncio.run(scenario()) == []
