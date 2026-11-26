"""A duplicate engine run must never take a job off the worker executing it (H9).

Two Hatchet runs can exist for one job id (an overlapping redispatch, two
reapers, a rerun of a queued row). The loser's claim used to raise "could not
be claimed", Hatchet retried it, and ``prepare_retry_attempt`` requeued any
``running`` row — the live run's included — so the pipeline ran twice and
whichever run finished first won. These tests force each interleaving.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import update

from app.core.exceptions import AppError
from app.database.models import JobRecord
from app.services.job_queue_service import JobQueueService
from tests.test_job_queue_hatchet_cancel import make_service
from tests.test_job_queue_hatchet_dispatch import _event_types, _stale_iso


async def _queued(service: JobQueueService, session_id: str = "session-1") -> str:
    await service.repository.initialize()
    job = await service.enqueue(session_id, "process_session", {}, auto_start=False)
    return str(job["id"])


async def _running_under(service: JobQueueService, worker: str, session_id: str = "session-1") -> str:
    job_id = await _queued(service, session_id)
    assert (await service.repository.claim_queued(job_id, worker)).claimed
    return job_id


async def _age_lock(service: JobQueueService, job_id: str, minutes: int) -> None:
    async with service.repository.database.transaction() as db:
        await db.execute(update(JobRecord).where(JobRecord.id == job_id).values(locked_at=_stale_iso(minutes)))


def _retry(service: JobQueueService, job_id: str, worker: str, stale_after: int = 900) -> Any:
    return service.repository.prepare_retry_attempt(
        job_id, "retry", worker_id=worker, stale_after_seconds=stale_after
    )


# --- the loser of a claim stands down ----------------------------------------


def test_duplicate_run_returns_quietly_while_another_worker_runs_the_job(tmp_path) -> None:
    service = make_service(tmp_path)

    async def scenario():
        job_id = await _running_under(service, "worker-A")
        result = await service.run_job(job_id, raise_on_error=True)  # must not raise
        return result, await service.repository.read(job_id), await _event_types(service, job_id)

    result, row, events = asyncio.run(scenario())

    assert result.job is None and result.error is None
    assert row["status"] == "running" and row["lockedBy"] == "worker-A" and row["attempts"] == 1
    assert "running" not in events


def test_a_finished_job_still_stands_down_without_raising(tmp_path) -> None:
    service = make_service(tmp_path)

    async def scenario():
        job_id = await _running_under(service, "worker-A")
        await service.repository.mark_succeeded(job_id, worker_id="worker-A")
        return await service.run_job(job_id, raise_on_error=True)

    assert asyncio.run(scenario()).job is None


def test_a_cancelled_unclaimable_job_still_raises_for_hatchet(tmp_path) -> None:
    """Only the *held* verdict was softened; a status Hatchet should look at
    again (here a waiting row) keeps raising."""
    service = make_service(tmp_path)

    async def scenario():
        await service.repository.initialize()
        job = await service.create_waiting_job("session-w", "process_session", {})
        return await service.run_job(str(job["id"]), raise_on_error=True)

    with pytest.raises(RuntimeError, match="could not be claimed"):
        asyncio.run(scenario())


# --- prepare_retry_attempt only requeues a row that is provably dead ----------


def test_retry_requeues_the_workers_own_dead_attempt(tmp_path) -> None:
    service = make_service(tmp_path)

    async def scenario():
        job_id = await _running_under(service, "worker-A")
        return await _retry(service, job_id, "worker-A")

    row = asyncio.run(scenario())
    assert row["status"] == "queued" and row["lockedBy"] is None and row["attempts"] == 1


def test_retry_requeues_a_row_with_a_stale_heartbeat_for_any_worker(tmp_path) -> None:
    service = make_service(tmp_path)

    async def scenario():
        job_id = await _running_under(service, "worker-A")
        await _age_lock(service, job_id, minutes=30)
        return await _retry(service, job_id, "worker-B", stale_after=900)

    assert asyncio.run(scenario())["status"] == "queued"


def test_retry_leaves_a_fresh_row_held_by_another_worker_alone(tmp_path) -> None:
    service = make_service(tmp_path)

    async def scenario():
        job_id = await _running_under(service, "worker-A")
        row = await _retry(service, job_id, "worker-B")
        return row, await service.repository.read(job_id)

    row, stored = asyncio.run(scenario())
    for current in (row, stored):
        assert current["status"] == "running" and current["lockedBy"] == "worker-A" and current["attempts"] == 1


def test_hatchet_retry_of_a_duplicate_run_does_not_announce_or_requeue(tmp_path) -> None:
    service = make_service(tmp_path)

    async def scenario():
        job_id = await _running_under(service, "worker-A")
        proceeds = await service.prepare_hatchet_retry_attempt(job_id, retry_count=1)
        return proceeds, await service.repository.read(job_id), await _event_types(service, job_id)

    proceeds, row, events = asyncio.run(scenario())
    assert proceeds is True
    assert row["status"] == "running" and row["lockedBy"] == "worker-A"
    assert "retry_prepared" not in events


def test_failed_rows_are_still_requeued_by_anyone(tmp_path) -> None:
    service = make_service(tmp_path)

    async def scenario():
        job_id = await _running_under(service, "worker-A")
        await service.repository.mark_failed(job_id, "boom", worker_id="worker-A")
        return await _retry(service, job_id, "worker-B")

    assert asyncio.run(scenario())["status"] == "queued"


# --- completion is fenced on the owner ----------------------------------------


def test_finish_by_a_non_owner_changes_nothing(tmp_path) -> None:
    service = make_service(tmp_path)

    async def scenario():
        job_id = await _running_under(service, "worker-A")
        succeeded = await service.repository.mark_succeeded(job_id, worker_id="worker-B")
        failed = await service.repository.mark_failed(job_id, "boom", worker_id="worker-B")
        return succeeded, failed, await service.repository.read(job_id)

    succeeded, failed, row = asyncio.run(scenario())
    assert succeeded is None and failed is None
    assert row["status"] == "running" and row["lockedBy"] == "worker-A"


class _StealingPipeline:
    """Runs the 'pipeline', letting a test change the row meanwhile."""

    def __init__(self, during, error: Exception | None = None) -> None:
        self.during = during
        self.error = error
        self.failed_sessions: list[str] = []

    async def process_session_by_id(self, _session_id: str, *, allow_processing: bool = False) -> dict[str, Any]:
        await self.during()
        if self.error is not None:
            raise self.error
        return {"ok": True}

    async def mark_session_failed(self, session_id: str, _error: Exception) -> None:
        self.failed_sessions.append(session_id)


def _run_with_pipeline(service: JobQueueService, during_factory, error: Exception | None = None):
    synced: list[str] = []
    original_sync = service._sync_session_job

    async def spy_sync(job: dict[str, Any]) -> None:
        synced.append(str(job.get("status")))
        await original_sync(job)

    service._sync_session_job = spy_sync  # type: ignore[method-assign]

    async def scenario():
        job_id = await _queued(service)

        async def during() -> None:
            await during_factory(service, job_id)

        pipeline = _StealingPipeline(during, error)
        service.bind_handlers(pipeline=pipeline, clips=object())
        synced.clear()
        result = await service.run_job(job_id, raise_on_error=True)
        return result, pipeline, await service.repository.read(job_id), await _event_types(service, job_id), list(synced)

    return asyncio.run(scenario())


async def _taken_over(service: JobQueueService, job_id: str) -> None:
    """The reaper requeued this run's row and another worker claimed it."""
    await service.repository.requeue_interrupted_job(job_id, "reaped")
    assert (await service.repository.claim_queued(job_id, "worker-B")).claimed


def test_success_of_a_run_that_lost_its_row_is_not_announced(tmp_path) -> None:
    result, pipeline, row, events, synced = _run_with_pipeline(make_service(tmp_path), _taken_over)

    assert result.job is None
    assert row["status"] == "running" and row["lockedBy"] == "worker-B"
    assert "succeeded" not in events
    assert synced == ["running"], "only the claim itself synced the session; the lost finish did not"


def test_failure_of_a_run_that_lost_its_row_does_not_fail_the_session(tmp_path) -> None:
    result, pipeline, row, events, _ = _run_with_pipeline(
        make_service(tmp_path), _taken_over, error=RuntimeError("late boom")
    )

    assert result.job is None and result.error is None
    assert row["status"] == "running" and row["lockedBy"] == "worker-B"
    assert "failed" not in events
    assert pipeline.failed_sessions == []


async def _cancel(service: JobQueueService, job_id: str) -> None:
    await service.repository.mark_cancelled(job_id, "Cancelled by user.")


def test_cancel_then_late_success_by_the_owner_stays_cancelled(tmp_path) -> None:
    result, _, row, events, synced = _run_with_pipeline(make_service(tmp_path), _cancel)

    assert result.job is None
    assert row["status"] == "cancelled"
    assert "succeeded" not in events
    assert "succeeded" not in synced


# --- rerun only restarts a finished job ----------------------------------------


@pytest.mark.parametrize("make_active", ["queued", "running"])
def test_rerun_refuses_a_job_already_in_flight(tmp_path, monkeypatch, make_active) -> None:
    service = make_service(tmp_path)
    dispatched: list[str] = []

    async def fake_dispatch(job: dict[str, Any]) -> None:
        dispatched.append(str(job["id"]))

    monkeypatch.setattr(service, "_dispatch", fake_dispatch)

    async def scenario():
        job_id = await _running_under(service, "worker-A") if make_active == "running" else await _queued(service)
        with pytest.raises(AppError) as refused:
            await service.rerun(job_id)
        return refused.value, await service.repository.read(job_id)

    error, row = asyncio.run(scenario())
    assert error.status_code == 409
    assert row["status"] == make_active
    assert dispatched == []


def test_rerun_refuses_when_another_active_job_covers_the_same_session_and_type(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    monkeypatch.setattr(service, "_dispatch", lambda _job: asyncio.sleep(0))

    async def scenario():
        failed_id = await _running_under(service, "worker-A")
        await service.repository.mark_failed(failed_id, "boom", worker_id="worker-A")
        sibling = await service.enqueue("session-1", "process_session", {}, auto_start=False)
        assert sibling["id"] != failed_id
        with pytest.raises(AppError) as refused:
            await service.rerun(failed_id)
        return refused.value, await service.repository.read(failed_id)

    error, row = asyncio.run(scenario())
    assert error.status_code == 409
    assert row["status"] == "failed"


@pytest.mark.parametrize("terminal", ["failed", "cancelled", "succeeded"])
def test_rerun_requeues_a_finished_job_with_no_active_sibling(tmp_path, monkeypatch, terminal) -> None:
    service = make_service(tmp_path)
    dispatched: list[str] = []

    async def fake_dispatch(job: dict[str, Any]) -> None:
        dispatched.append(str(job["id"]))

    monkeypatch.setattr(service, "_dispatch", fake_dispatch)

    async def scenario():
        job_id = await _running_under(service, "worker-A")
        if terminal == "failed":
            await service.repository.mark_failed(job_id, "boom", worker_id="worker-A")
        elif terminal == "cancelled":
            await service.repository.mark_cancelled(job_id, "stop")
        else:
            await service.repository.mark_succeeded(job_id, worker_id="worker-A")
        return job_id, await service.rerun(job_id)

    job_id, row = asyncio.run(scenario())
    assert row["status"] == "queued" and row["attempts"] == 0
    assert dispatched == [job_id]
