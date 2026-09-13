"""Hatchet-side cancellation and the missing-job race it used to leave behind.

Bug: ``cancel()`` (and ``purge_session``, which calls it) only ever touched the
*local* job row — it marked it cancelled, or deleted it outright, but never
told the Hatchet engine to abort the step run it had dispatched. A run already
handed to Hatchet kept executing or retrying independently, and when a retry
eventually landed for a job id that ``purge_session`` had deleted,
``prepare_hatchet_retry_attempt`` raised an unhandled ``FileNotFoundError`` and
crashed the Hatchet task.

These tests pin both halves of the fix:
  * ``cancel()`` makes a best-effort engine-side abort call for a job that
    carries Hatchet run metadata, and never lets a transport failure there
    block the local cancellation.
  * ``prepare_hatchet_retry_attempt`` / ``_execute_job`` / ``process_job``
    treat a job row that no longer exists as "already gone" and stop quietly,
    instead of raising.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.config import Settings
from sqlalchemy import select

from app.database.models import JobEventRecord
from app.database.orm import OrmDatabase
from app.repositories.job_repository import JobRepository
from app.services.event_service import EventService
from app.services.job_queue_service import JobQueueService, JobRunResult
from tests.fixtures.session_store import SessionUpdateMixin


class FakeSessions(SessionUpdateMixin):
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}

    async def read(self, session_id: str) -> dict[str, Any]:
        if session_id not in self.sessions:
            self.sessions[session_id] = {"id": session_id, "status": "processing"}
        return dict(self.sessions[session_id])

    async def write(self, session: dict[str, Any]) -> None:
        self.sessions[str(session["id"])] = dict(session)


class FakeStorage:
    async def prepare_session_sources(self, session: dict[str, Any]) -> dict[str, Any]:
        return session


class FakeRunsClient:
    """Stand-in for ``hatchet.runs`` — records cancel calls, can be told to fail."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.raises = raises
        self.cancelled_run_ids: list[str] = []

    async def aio_cancel(self, run_id: str) -> None:
        self.cancelled_run_ids.append(run_id)
        if self.raises is not None:
            raise self.raises


class FakeHatchetClient:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.runs = FakeRunsClient(raises=raises)


def make_service(tmp_path, **setting_overrides: Any) -> JobQueueService:
    setting_overrides.setdefault("job_queue_backend", "hatchet")
    settings = Settings(
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        **setting_overrides,
    )
    repository = JobRepository(OrmDatabase(tmp_path / "osce_marker.sqlite3"))
    service = JobQueueService(settings, repository, EventService(settings), FakeSessions(), FakeStorage())
    service.bind_handlers(pipeline=object(), clips=object())
    return service


async def _enqueue_dispatched_job(service: JobQueueService, session_id: str, run_id: str = "run-abc") -> dict[str, Any]:
    """A job already handed to Hatchet: queued, carrying run metadata."""
    await service.repository.initialize()
    job = await service.enqueue(session_id, "process_session", {}, auto_start=False)
    payload = dict(job.get("payload") or {})
    payload["hatchet"] = {"runId": run_id, "dispatchedAt": "2026-01-01T00:00:00Z", "task": "osce-process-job"}
    job["payload"] = payload
    await service.repository.write(job)
    return await service.repository.read(str(job["id"]))


async def _job_event_types(service: JobQueueService, job_id: str) -> list[str]:
    async with service.repository.database.session() as db:
        rows = await db.scalars(
            select(JobEventRecord.event_type)
            .where(JobEventRecord.job_id == job_id)
            .order_by(JobEventRecord.id.asc())
        )
        return [str(event_type) for event_type in rows]


# --- cancel() aborts the Hatchet run -----------------------------------------


def test_cancel_aborts_the_dispatched_hatchet_run(tmp_path, monkeypatch) -> None:
    fake_client = FakeHatchetClient()
    monkeypatch.setattr("app.queue.hatchet_tasks.hatchet", fake_client)
    service = make_service(tmp_path)

    async def scenario() -> dict[str, Any]:
        job = await _enqueue_dispatched_job(service, "session-1", run_id="run-xyz")
        return await service.cancel(str(job["id"]), "Session deleted.")

    cancelled = asyncio.run(scenario())

    assert cancelled["status"] == "cancelled"
    assert fake_client.runs.cancelled_run_ids == ["run-xyz"]


def test_cancel_on_local_backend_never_touches_hatchet(tmp_path, monkeypatch) -> None:
    fake_client = FakeHatchetClient()
    monkeypatch.setattr("app.queue.hatchet_tasks.hatchet", fake_client)
    service = make_service(tmp_path, job_queue_backend="local")

    async def scenario() -> dict[str, Any]:
        job = await _enqueue_dispatched_job(service, "session-1")
        return await service.cancel(str(job["id"]), "Session deleted.")

    cancelled = asyncio.run(scenario())

    assert cancelled["status"] == "cancelled"
    assert fake_client.runs.cancelled_run_ids == []


def test_cancel_skips_hatchet_call_when_job_was_never_dispatched(tmp_path, monkeypatch) -> None:
    fake_client = FakeHatchetClient()
    monkeypatch.setattr("app.queue.hatchet_tasks.hatchet", fake_client)
    service = make_service(tmp_path)

    async def scenario() -> dict[str, Any]:
        await service.repository.initialize()
        job = await service.enqueue("session-1", "process_session", {}, auto_start=False)
        return await service.cancel(str(job["id"]), "Session deleted.")

    cancelled = asyncio.run(scenario())

    assert cancelled["status"] == "cancelled"
    assert fake_client.runs.cancelled_run_ids == []


def test_cancel_completes_locally_even_if_the_hatchet_abort_fails(tmp_path, monkeypatch) -> None:
    """The row is already cancelled; a dead Hatchet API must not block that or
    strand the caller (often a session purge) mid-way."""
    fake_client = FakeHatchetClient(raises=RuntimeError("hatchet unreachable"))
    monkeypatch.setattr("app.queue.hatchet_tasks.hatchet", fake_client)
    service = make_service(tmp_path)

    async def scenario() -> tuple[dict[str, Any], list[str]]:
        job = await _enqueue_dispatched_job(service, "session-1", run_id="run-down")
        cancelled = await service.cancel(str(job["id"]), "Session deleted.")
        events = await _job_event_types(service, str(job["id"]))
        return cancelled, events

    cancelled, events = asyncio.run(scenario())

    assert cancelled["status"] == "cancelled"
    assert fake_client.runs.cancelled_run_ids == ["run-down"]
    assert "hatchet_cancel_failed" in events


def test_purge_session_cancels_the_hatchet_run_before_deleting_the_row(tmp_path, monkeypatch) -> None:
    fake_client = FakeHatchetClient()
    monkeypatch.setattr("app.queue.hatchet_tasks.hatchet", fake_client)
    service = make_service(tmp_path)

    async def scenario() -> int:
        job = await _enqueue_dispatched_job(service, "session-purge", run_id="run-purged")
        # Simulate the row reaching "running" the way claim_queued would.
        await service.repository.claim_queued(str(job["id"]), "worker-1")
        return await service.purge_session("session-purge")

    deleted_count = asyncio.run(scenario())

    assert deleted_count == 1
    assert fake_client.runs.cancelled_run_ids == ["run-purged"]


# --- prepare_hatchet_retry_attempt against a purged job ----------------------


def test_prepare_retry_attempt_returns_false_for_a_purged_job(tmp_path) -> None:
    service = make_service(tmp_path)

    async def scenario() -> bool:
        await service.repository.initialize()
        return await service.prepare_hatchet_retry_attempt("job-does-not-exist", retry_count=1)

    still_exists = asyncio.run(scenario())

    assert still_exists is False


def test_prepare_retry_attempt_first_attempt_is_a_no_op(tmp_path) -> None:
    """``retry_count <= 0`` means "not a retry"; it must not touch the row and
    must report the job as present without checking."""
    service = make_service(tmp_path)

    async def scenario() -> bool:
        await service.repository.initialize()
        return await service.prepare_hatchet_retry_attempt("job-does-not-exist", retry_count=0)

    still_exists = asyncio.run(scenario())

    assert still_exists is True


def test_prepare_retry_attempt_succeeds_for_an_existing_failed_job(tmp_path) -> None:
    service = make_service(tmp_path)

    async def scenario() -> tuple[bool, dict[str, Any]]:
        job = await _enqueue_dispatched_job(service, "session-retry")
        await service.repository.claim_queued(str(job["id"]), "worker-1")
        await service.repository.mark_failed(str(job["id"]), "transient")
        still_exists = await service.prepare_hatchet_retry_attempt(str(job["id"]), retry_count=1)
        return still_exists, await service.repository.read(str(job["id"]))

    still_exists, updated = asyncio.run(scenario())

    assert still_exists is True
    assert updated["status"] == "queued"


# --- _execute_job / run_job against a purged job -----------------------------


def test_execute_job_no_ops_when_the_row_was_purged_between_dispatch_and_claim(tmp_path) -> None:
    service = make_service(tmp_path)

    async def scenario() -> JobRunResult:
        await service.repository.initialize()
        return await service.run_job("job-does-not-exist", raise_on_error=True)

    result = asyncio.run(scenario())

    assert result.failed is False
    assert result.job is None
