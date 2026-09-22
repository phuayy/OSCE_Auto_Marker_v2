"""Recording a Hatchet dispatch must never undo a concurrent claim (F1).

``_dispatch_hatchet`` used to persist the engine's run id by writing the whole
in-memory job dict back through ``JobRepository.write`` — an unconditional
``merge()`` of every column. ``enqueue_process_job`` returns as soon as the
engine accepts the run, and by then a worker may already have claimed the row
(``status=running``, ``locked_by`` set). The stale snapshot then put the row
back to ``queued`` with no lock: the live worker's heartbeat stopped matching,
the reaper requeued and redispatched it, and the pipeline ran a second time.

The same read-then-``merge()`` shape existed at every site that strips stale
dispatch metadata before redispatching (``rerun``, the stale branch of
``redispatch_stale_hatchet_jobs``, ``reap_stale_jobs``). These tests force the
claim to land inside each window and assert the claim wins.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.database.models import JobEventRecord
from app.services.job_queue_service import JobQueueService
from tests.test_job_queue_hatchet_cancel import make_service


class FakeRunRef:
    def __init__(self, run_id: str) -> None:
        self.workflow_run_id = run_id


class RecordingEnqueue:
    """Stand-in for ``enqueue_process_job`` that can run a hook *before*
    returning — i.e. inside the window between the engine accepting the run
    and the dispatcher persisting its metadata."""

    def __init__(self, run_id: str = "run-1", on_enqueue=None) -> None:
        self.run_id = run_id
        self.on_enqueue = on_enqueue
        self.calls: list[str] = []

    async def __call__(self, job_id: str) -> FakeRunRef:
        self.calls.append(job_id)
        if self.on_enqueue is not None:
            await self.on_enqueue(job_id)
        return FakeRunRef(self.run_id)


def _stale_iso(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


async def _event_types(service: JobQueueService, job_id: str) -> list[str]:
    async with service.repository.database.session() as db:
        rows = await db.scalars(
            select(JobEventRecord.event_type).where(JobEventRecord.job_id == job_id).order_by(JobEventRecord.id.asc())
        )
        return [str(row) for row in rows]


# --- enqueue -> dispatch -----------------------------------------------------


def test_dispatch_does_not_undo_a_claim_that_lands_before_the_metadata_write(tmp_path, monkeypatch) -> None:
    """The bug itself: the worker claims while the dispatcher is still holding
    the pre-claim snapshot. The claim must survive, and the run id must still
    be recorded so ``cancel`` can abort the engine run later."""
    service = make_service(tmp_path)

    async def claim_mid_dispatch(job_id: str) -> None:
        claim = await service.repository.claim_queued(job_id, "worker-1")
        assert claim.claimed

    enqueue = RecordingEnqueue(run_id="run-raced", on_enqueue=claim_mid_dispatch)
    monkeypatch.setattr("app.queue.hatchet_tasks.enqueue_process_job", enqueue)

    async def scenario() -> tuple[dict[str, Any], list[str]]:
        await service.repository.initialize()
        job = await service.enqueue("session-1", "process_session", {"workflow": "standard"}, auto_start=True)
        job_id = str(job["id"])
        return await service.repository.read(job_id), await _event_types(service, job_id)

    row, events = asyncio.run(scenario())

    assert enqueue.calls == [row["id"]]
    assert row["status"] == "running", "a stale queued snapshot must never overwrite a live claim"
    assert row["lockedBy"] == "worker-1"
    assert row["lockedAt"] is not None
    assert row["attempts"] == 1
    assert row["payload"]["workflow"] == "standard"
    assert row["payload"]["hatchet"]["runId"] == "run-raced"
    assert "dispatched" in events


def test_dispatch_records_metadata_on_the_row_when_no_claim_intervenes(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    enqueue = RecordingEnqueue(run_id="run-plain")
    monkeypatch.setattr("app.queue.hatchet_tasks.enqueue_process_job", enqueue)

    async def scenario() -> dict[str, Any]:
        await service.repository.initialize()
        job = await service.enqueue("session-1", "process_session", {}, auto_start=True)
        return await service.repository.read(str(job["id"]))

    row = asyncio.run(scenario())

    assert row["status"] == "queued"
    assert row["lockedBy"] is None
    assert row["payload"]["hatchet"]["runId"] == "run-plain"
    assert row["payload"]["hatchet"]["task"] == "osce-process-job"
    assert row["payload"]["hatchet"]["dispatchedAt"]


def test_dispatch_survives_the_row_being_purged_before_the_metadata_write(tmp_path, monkeypatch) -> None:
    """A session deleted between the engine accepting the run and the write:
    nothing to record, nothing to raise, and no orphan row recreated by the
    old ``merge()`` (which would happily re-insert a deleted job)."""
    service = make_service(tmp_path)

    async def purge_mid_dispatch(_job_id: str) -> None:
        await service.repository.delete_for_session("session-gone")

    enqueue = RecordingEnqueue(on_enqueue=purge_mid_dispatch)
    monkeypatch.setattr("app.queue.hatchet_tasks.enqueue_process_job", enqueue)

    async def scenario() -> list[dict[str, Any]]:
        await service.repository.initialize()
        await service.enqueue("session-gone", "process_session", {}, auto_start=True)
        return await service.repository.list_for_session("session-gone")

    assert asyncio.run(scenario()) == []


# --- rerun -------------------------------------------------------------------


def test_rerun_clears_old_dispatch_metadata_and_records_the_new_run(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path)
    enqueue = RecordingEnqueue(run_id="run-second")
    monkeypatch.setattr("app.queue.hatchet_tasks.enqueue_process_job", enqueue)

    async def scenario() -> dict[str, Any]:
        await service.repository.initialize()
        job = await service.enqueue("session-1", "process_session", {}, auto_start=False)
        job_id = str(job["id"])
        await service.repository.record_dispatch(job_id, {"runId": "run-first", "dispatchedAt": _stale_iso(0)})
        await service.repository.claim_queued(job_id, "worker-1")
        await service.repository.mark_failed(job_id, "boom")
        await service.rerun(job_id)
        return await service.repository.read(job_id)

    row = asyncio.run(scenario())

    assert enqueue.calls == [row["id"]]
    assert row["status"] == "queued"
    assert row["payload"]["hatchet"]["runId"] == "run-second"


# --- redispatch_stale_hatchet_jobs --------------------------------------------


def test_redispatch_stale_leaves_a_job_claimed_after_the_scan_alone(tmp_path, monkeypatch) -> None:
    """The scan's snapshot says ``queued`` with a stale dispatch; the run the
    scan thought was dead claims the row before the metadata is cleared. The
    claim must stand, its metadata must stay, and no second run may be
    dispatched."""
    service = make_service(tmp_path, hatchet_job_schedule_timeout_minutes=1)
    enqueue = RecordingEnqueue(run_id="run-duplicate")
    monkeypatch.setattr("app.queue.hatchet_tasks.enqueue_process_job", enqueue)

    original_list_queued = service.repository.list_queued

    async def list_then_claim() -> list[dict[str, Any]]:
        snapshot = await original_list_queued()
        for job in snapshot:
            claim = await service.repository.claim_queued(str(job["id"]), "worker-live")
            assert claim.claimed
        return snapshot

    monkeypatch.setattr(service.repository, "list_queued", list_then_claim)

    async def scenario() -> dict[str, Any]:
        await service.repository.initialize()
        job = await service.enqueue("session-1", "process_session", {}, auto_start=False)
        job_id = str(job["id"])
        await service.repository.record_dispatch(job_id, {"runId": "run-slow", "dispatchedAt": _stale_iso(30)})
        await service.redispatch_stale_hatchet_jobs()
        return await service.repository.read(job_id)

    row = asyncio.run(scenario())

    assert enqueue.calls == []
    assert row["status"] == "running"
    assert row["lockedBy"] == "worker-live"
    assert row["payload"]["hatchet"]["runId"] == "run-slow"


def test_redispatch_stale_redispatches_a_genuinely_stale_queued_job(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path, hatchet_job_schedule_timeout_minutes=1)
    enqueue = RecordingEnqueue(run_id="run-fresh")
    monkeypatch.setattr("app.queue.hatchet_tasks.enqueue_process_job", enqueue)

    async def scenario() -> tuple[dict[str, Any], list[str]]:
        await service.repository.initialize()
        job = await service.enqueue("session-1", "process_session", {}, auto_start=False)
        job_id = str(job["id"])
        await service.repository.record_dispatch(job_id, {"runId": "run-lost", "dispatchedAt": _stale_iso(30)})
        await service.redispatch_stale_hatchet_jobs()
        return await service.repository.read(job_id), await _event_types(service, job_id)

    row, events = asyncio.run(scenario())

    assert enqueue.calls == [row["id"]]
    assert row["status"] == "queued"
    assert row["payload"]["hatchet"]["runId"] == "run-fresh"
    assert "redispatch_stale" in events


# --- reap_stale_jobs ----------------------------------------------------------


def test_reap_stale_leaves_a_job_reclaimed_after_the_requeue_alone(tmp_path, monkeypatch) -> None:
    """Between the reaper's requeue and its metadata strip, another worker
    claims the freshly queued row. The reaper must not clobber that claim or
    dispatch on top of it."""
    service = make_service(tmp_path, job_stale_running_timeout_seconds=0)
    enqueue = RecordingEnqueue(run_id="run-duplicate")
    monkeypatch.setattr("app.queue.hatchet_tasks.enqueue_process_job", enqueue)

    original_requeue = service.repository.requeue_interrupted_job

    async def requeue_then_claim(job_id: str, reason: str) -> dict[str, Any]:
        requeued = await original_requeue(job_id, reason)
        claim = await service.repository.claim_queued(job_id, "worker-2")
        assert claim.claimed
        return requeued

    monkeypatch.setattr(service.repository, "requeue_interrupted_job", requeue_then_claim)

    async def scenario() -> tuple[list[str], dict[str, Any]]:
        await service.repository.initialize()
        job = await service.enqueue("session-1", "process_session", {}, auto_start=False)
        job_id = str(job["id"])
        await service.repository.record_dispatch(job_id, {"runId": "run-dead", "dispatchedAt": _stale_iso(0)})
        await service.repository.claim_queued(job_id, "dead-worker")
        reaped = await service.reap_stale_jobs()
        return reaped, await service.repository.read(job_id)

    reaped, row = asyncio.run(scenario())

    assert reaped == []
    assert enqueue.calls == []
    assert row["status"] == "running"
    assert row["lockedBy"] == "worker-2"


def test_reap_stale_strips_metadata_and_redispatches_an_orphaned_job(tmp_path, monkeypatch) -> None:
    service = make_service(tmp_path, job_stale_running_timeout_seconds=0)
    enqueue = RecordingEnqueue(run_id="run-resumed")
    monkeypatch.setattr("app.queue.hatchet_tasks.enqueue_process_job", enqueue)

    async def scenario() -> tuple[list[str], dict[str, Any]]:
        await service.repository.initialize()
        job = await service.enqueue("session-1", "process_session", {}, auto_start=False)
        job_id = str(job["id"])
        await service.repository.record_dispatch(job_id, {"runId": "run-dead", "dispatchedAt": _stale_iso(0)})
        await service.repository.claim_queued(job_id, "dead-worker")
        reaped = await service.reap_stale_jobs()
        return reaped, await service.repository.read(job_id)

    reaped, row = asyncio.run(scenario())

    assert reaped == [row["id"]]
    assert enqueue.calls == [row["id"]]
    assert row["status"] == "queued"
    assert row["lockedBy"] is None
    assert row["payload"]["hatchet"]["runId"] == "run-resumed"
