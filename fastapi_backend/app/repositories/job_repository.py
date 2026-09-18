from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.json_utils import read_json_file
from app.core.utils import utc_now_iso
from app.database.models import JobAttemptRecord, JobEventRecord, JobRecord
from app.database.orm import OrmDatabase
from app.domain.jobs import ACTIVE_JOB_STATUSES, RECOVERABLE_JOB_STATUSES, JobStatus


# Attempt rows are closed with the same status word the job ended on, except an
# attempt cut short by a restart, which is neither a success nor a failure of
# the work itself.
ATTEMPT_INTERRUPTED = "interrupted"
ATTEMPT_RUNNING = "running"


@dataclass(frozen=True)
class JobClaim:
    """Outcome of trying to take a queued job for execution.

    ``claimed``    — ``job`` is now ``running`` under this worker.
    ``not_queued`` — the row is no longer queued (another worker took it, it was
                     cancelled, or it already finished); ``job`` is the current row.
    ``exhausted``  — the row was queued but had no attempts left, so it has just
                     been marked ``failed``; ``job`` is that failed row. The caller
                     owns the consequence: a session waiting on this job must be
                     failed too, or it sits in flight forever.
    """

    outcome: str
    job: dict[str, Any]

    @property
    def claimed(self) -> bool:
        return self.outcome == "claimed"

    @property
    def exhausted(self) -> bool:
        return self.outcome == "exhausted"


class JobRepository:
    """The queue's storage, on the same SQLAlchemy engine as everything else.

    This layer used to run on a second, raw-SQL connection pool of its own
    (``app/database/connection.py``) with its schema written out by hand in
    ``app/database/schema.py``. One database with two pools and two schema
    sources meant Alembic had to be told to ignore half of it, ``alembic check``
    could not detect drift there, and a PostgreSQL deployment opened two sets of
    connections for one application. The tables are ordinary models now
    (``JobRecord`` and friends); the SQL below is the same SQL, expressed once.

    Two properties the queue depends on and that survived the move verbatim:

    * **A claim is a conditional update, not a read-then-write.** Whoever's
      ``UPDATE ... WHERE status = 'queued'`` matches a row owns the job; a
      second worker's matches nothing. That is what makes the claim atomic on
      SQLite and PostgreSQL alike without either a lock table or
      ``SELECT FOR UPDATE``.
    * **Timestamps are ISO-8601 UTC text.** They are compared and ordered as
      strings and travel to the browser as-is; see ``JobRecord``.
    """

    def __init__(self, database: OrmDatabase | Path, legacy_jobs_dir: Path | None = None) -> None:
        if isinstance(database, OrmDatabase):
            self.database = database
            self.legacy_jobs_dir = legacy_jobs_dir
        else:
            # Legacy call shape: a jobs directory of JSON files, with the
            # database beside it at the layout's usual place.
            self.database = OrmDatabase(database.parent / "database" / "osce_marker.sqlite3")
            self.legacy_jobs_dir = database

    async def initialize(self) -> None:
        await self.database.initialize()
        await self.migrate_legacy_jobs()

    async def migrate_legacy_jobs(self) -> int:
        if self.legacy_jobs_dir is None or not self.legacy_jobs_dir.exists():
            return 0

        migrated = 0
        for path in self.legacy_jobs_dir.glob("*.json"):
            payload = await asyncio.to_thread(read_json_file, path)
            if not isinstance(payload, dict) or not payload.get("id"):
                continue
            if await self.exists(str(payload["id"])):
                continue
            await self.write(payload)
            migrated += 1
        return migrated

    # --- reads ---------------------------------------------------------------

    async def exists(self, job_id: str) -> bool:
        async with self.database.session() as db:
            found = await db.scalar(select(JobRecord.id).where(JobRecord.id == job_id))
            return found is not None

    async def read(self, job_id: str) -> dict[str, Any]:
        async with self.database.session() as db:
            return _to_job_dict(await self._require(db, job_id))

    async def read_all(self) -> list[dict[str, Any]]:
        async with self.database.session() as db:
            rows = await db.scalars(select(JobRecord).order_by(JobRecord.created_at.desc()))
            return [_to_job_dict(row) for row in rows]

    async def list_for_session(self, session_id: str) -> list[dict[str, Any]]:
        async with self.database.session() as db:
            rows = await db.scalars(
                select(JobRecord)
                .where(JobRecord.session_id == session_id)
                .order_by(JobRecord.created_at.desc())
            )
            return [_to_job_dict(row) for row in rows]

    async def list_queued(self) -> list[dict[str, Any]]:
        async with self.database.session() as db:
            return [_to_job_dict(row) for row in await db.scalars(_queued_in_order())]

    async def find_active(self, session_id: str, task_type: str) -> dict[str, Any] | None:
        async with self.database.session() as db:
            row = await db.scalar(
                select(JobRecord)
                .where(
                    JobRecord.session_id == session_id,
                    JobRecord.task_type == task_type,
                    JobRecord.status.in_(sorted(ACTIVE_JOB_STATUSES)),
                )
                .order_by(JobRecord.created_at.desc())
                .limit(1)
            )
            return _to_job_dict(row) if row is not None else None

    async def active_session_ids(self) -> set[str]:
        """Sessions that some job row still intends to run.

        The startup reconciliation compares this against sessions whose status
        claims a job is working on them; a session in that state with no row
        here has nothing left that could ever move it.
        """
        async with self.database.session() as db:
            rows = await db.scalars(
                select(JobRecord.session_id)
                .where(JobRecord.status.in_(sorted(ACTIVE_JOB_STATUSES)))
                .distinct()
            )
            return {str(session_id) for session_id in rows}

    # --- writes --------------------------------------------------------------

    async def write(self, job: dict[str, Any]) -> None:
        """Insert the job, or replace every column of an existing one.

        ``merge`` rather than a dialect-specific upsert: the statement has to
        work on both backends, and this is not a hot path (enqueue, and the
        one-off legacy import).
        """
        columns = _columns_from_job_dict(job)
        async with self.database.transaction() as db:
            await db.merge(JobRecord(**columns))

    async def delete_for_session(self, session_id: str) -> int:
        """Delete every job for a session plus its attempt/event rows.

        Keep the explicit child-first deletion on both backends. Runtime SQLite
        connections now enforce foreign keys as PostgreSQL does; cascades also
        protect writes that bypass this repository.
        """
        async with self.database.transaction() as db:
            job_ids = list(
                await db.scalars(select(JobRecord.id).where(JobRecord.session_id == session_id))
            )
            if not job_ids:
                return 0
            for model in (JobAttemptRecord, JobEventRecord):
                await db.execute(
                    delete(model)
                    .where(model.job_id.in_(job_ids))
                    .execution_options(synchronize_session=False)
                )
            await db.execute(
                delete(JobRecord)
                .where(JobRecord.session_id == session_id)
                .execution_options(synchronize_session=False)
            )
            return len(job_ids)

    async def recover_interrupted_jobs(self) -> list[dict[str, Any]]:
        """Requeue everything a stopped process left ``running``, then return the
        whole queue in dispatch order."""
        now = utc_now_iso()
        async with self.database.transaction() as db:
            await db.execute(
                update(JobRecord)
                .where(JobRecord.status == JobStatus.RUNNING)
                .values(
                    status=JobStatus.QUEUED,
                    queued_at=func.coalesce(JobRecord.queued_at, now),
                    started_at=None,
                    ended_at=None,
                    requeued_at=now,
                    locked_by=None,
                    locked_at=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            return [_to_job_dict(row) for row in await db.scalars(_queued_in_order())]

    async def requeue_interrupted_job(self, job_id: str, reason: str) -> dict[str, Any]:
        now = utc_now_iso()
        async with self.database.transaction() as db:
            row = await self._require(db, job_id)
            if row.status == JobStatus.RUNNING:
                attempt_number = int(row.attempts or 0)
                await db.execute(
                    update(JobRecord)
                    .where(JobRecord.id == job_id, JobRecord.status == JobStatus.RUNNING)
                    .values(
                        status=JobStatus.QUEUED,
                        queued_at=func.coalesce(JobRecord.queued_at, now),
                        started_at=None,
                        ended_at=None,
                        requeued_at=now,
                        locked_by=None,
                        locked_at=None,
                        error=reason,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                await self._close_open_attempt(
                    db, job_id, attempt_number, status=ATTEMPT_INTERRUPTED, ended_at=now, error=reason
                )
                await db.refresh(row)
            return _to_job_dict(row)

    async def prepare_retry_attempt(self, job_id: str, reason: str) -> dict[str, Any]:
        now = utc_now_iso()
        async with self.database.transaction() as db:
            row = await self._require(db, job_id)
            status = row.status
            attempts = int(row.attempts or 0)
            if status in RECOVERABLE_JOB_STATUSES and attempts < int(row.max_attempts or 1):
                await db.execute(
                    update(JobRecord)
                    .where(
                        JobRecord.id == job_id,
                        JobRecord.status.in_(sorted(RECOVERABLE_JOB_STATUSES)),
                        JobRecord.attempts < JobRecord.max_attempts,
                    )
                    .values(
                        status=JobStatus.QUEUED,
                        queued_at=now,
                        started_at=None,
                        ended_at=None,
                        requeued_at=now,
                        locked_by=None,
                        locked_at=None,
                        error=reason,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if status == JobStatus.RUNNING and attempts > 0:
                    await self._close_open_attempt(
                        db, job_id, attempts, status=ATTEMPT_INTERRUPTED, ended_at=now, error=reason
                    )
                await db.refresh(row)
            return _to_job_dict(row)

    async def claim_queued(self, job_id: str, worker_id: str) -> JobClaim:
        now = utc_now_iso()
        async with self.database.transaction() as db:
            row = await self._require(db, job_id)
            if row.status != JobStatus.QUEUED:
                return JobClaim("not_queued", _to_job_dict(row))

            if int(row.attempts or 0) >= int(row.max_attempts or 1):
                # Queued with nothing left to try: the row can never run, so it
                # is failed here and the caller is told, because the session
                # waiting on it has to be failed too.
                await db.execute(
                    update(JobRecord)
                    .where(JobRecord.id == job_id, JobRecord.status == JobStatus.QUEUED)
                    .values(
                        status=JobStatus.FAILED,
                        ended_at=now,
                        error="Maximum retry attempts reached.",
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                await db.refresh(row)
                outcome = "exhausted" if row.status == JobStatus.FAILED else "not_queued"
                return JobClaim(outcome, _to_job_dict(row))

            next_attempt = int(row.attempts or 0) + 1
            # The predicate is the lock: exactly one concurrent claimer's UPDATE
            # matches a queued row, and every other one matches nothing.
            result = await db.execute(
                update(JobRecord)
                .where(JobRecord.id == job_id, JobRecord.status == JobStatus.QUEUED)
                .values(
                    status=JobStatus.RUNNING,
                    attempts=next_attempt,
                    started_at=now,
                    ended_at=None,
                    error=None,
                    locked_by=worker_id,
                    locked_at=now,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            await db.refresh(row)
            if (
                result.rowcount != 1
                or row.status != JobStatus.RUNNING
                or str(row.locked_by or "") != worker_id
            ):
                return JobClaim("not_queued", _to_job_dict(row))

            db.add(
                JobAttemptRecord(
                    job_id=job_id,
                    attempt_number=next_attempt,
                    worker_id=worker_id,
                    status=ATTEMPT_RUNNING,
                    started_at=now,
                )
            )
            return JobClaim("claimed", _to_job_dict(row))

    async def touch_heartbeat(self, job_id: str, worker_id: str) -> bool:
        """Refresh ``locked_at`` for a job this worker is still actively
        running. Returns whether the row still matched — a job cancelled or
        reclaimed out from under a stale heartbeat loop should stop quietly,
        not keep touching a row it no longer owns.

        The conditional UPDATE (status + locked_by) is the same shape
        :meth:`claim_queued` uses to make its own write safe without a lock:
        whoever's predicate matches is the only writer that mattered.
        """
        now = utc_now_iso()
        async with self.database.transaction() as db:
            result = await db.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == job_id,
                    JobRecord.status == JobStatus.RUNNING,
                    JobRecord.locked_by == worker_id,
                )
                .values(locked_at=now, updated_at=now)
                .execution_options(synchronize_session=False)
            )
        return result.rowcount == 1

    async def list_stale_running(self, older_than_seconds: int) -> list[dict[str, Any]]:
        """Rows claimed as ``running`` whose heartbeat has gone quiet for
        longer than ``older_than_seconds`` — evidence the worker that claimed
        them is gone (crashed task, dead process), not merely that the step
        is taking a while: a live worker refreshes ``locked_at`` continuously
        while it runs (see ``JobQueueService._heartbeat_loop``), so a stale
        timestamp here only happens once that stopped.
        """
        cutoff = _iso_seconds_ago(older_than_seconds)
        async with self.database.session() as db:
            rows = await db.scalars(
                select(JobRecord).where(
                    JobRecord.status == JobStatus.RUNNING,
                    JobRecord.locked_at < cutoff,
                )
            )
            return [_to_job_dict(row) for row in rows]

    async def rerun(self, job_id: str, *, reset_attempts: bool = True) -> dict[str, Any]:
        now = utc_now_iso()
        async with self.database.transaction() as db:
            row = await self._require(db, job_id)
            if row.status == JobStatus.RUNNING:
                raise ValueError("Running jobs cannot be rerun until they finish or are cancelled.")
            row.status = JobStatus.QUEUED
            row.attempts = 0 if reset_attempts else int(row.attempts or 0)
            row.queued_at = now
            row.started_at = None
            row.ended_at = None
            row.requeued_at = now
            row.locked_by = None
            row.locked_at = None
            row.error = None
            row.updated_at = now
            return _to_job_dict(row)

    async def mark_succeeded(self, job_id: str) -> dict[str, Any]:
        return await self._finish(job_id, JobStatus.SUCCEEDED, None)

    async def mark_failed(self, job_id: str, error: str) -> dict[str, Any]:
        return await self._finish(job_id, JobStatus.FAILED, error or "Job failed.")

    async def mark_cancelled(self, job_id: str, reason: str) -> dict[str, Any]:
        return await self._finish(job_id, JobStatus.CANCELLED, reason or "Job cancelled.")

    async def append_event(
        self,
        job_id: str,
        event_type: str,
        message: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        async with self.database.transaction() as db:
            db.add(
                JobEventRecord(
                    job_id=job_id,
                    event_type=event_type,
                    message=message,
                    payload_json=_dump_json(payload or {}),
                    created_at=utc_now_iso(),
                )
            )

    # --- internals -----------------------------------------------------------

    async def _finish(self, job_id: str, status: str, error: str | None) -> dict[str, Any]:
        now = utc_now_iso()
        async with self.database.transaction() as db:
            row = await self._require(db, job_id)
            attempt_number = int(row.attempts or 0)
            row.status = status
            row.ended_at = now
            row.error = error
            row.locked_by = None
            row.locked_at = None
            row.updated_at = now
            if attempt_number > 0:
                await self._close_open_attempt(
                    db, job_id, attempt_number, status=status, ended_at=now, error=error
                )
            return _to_job_dict(row)

    @staticmethod
    async def _require(db: AsyncSession, job_id: str) -> JobRecord:
        row = await db.get(JobRecord, job_id)
        if row is None:
            raise FileNotFoundError(f"Job not found: {job_id}")
        return row

    @staticmethod
    async def _close_open_attempt(
        db: AsyncSession,
        job_id: str,
        attempt_number: int,
        *,
        status: str,
        ended_at: str,
        error: str | None,
    ) -> None:
        """End the attempt row this job is currently on, if it is still open.

        ``ended_at IS NULL`` keeps this idempotent: an attempt already closed by
        whoever got here first is left exactly as it was recorded.
        """
        await db.execute(
            update(JobAttemptRecord)
            .where(
                JobAttemptRecord.job_id == job_id,
                JobAttemptRecord.attempt_number == attempt_number,
                JobAttemptRecord.ended_at.is_(None),
            )
            .values(status=status, ended_at=ended_at, error=error)
            .execution_options(synchronize_session=False)
        )


def _iso_seconds_ago(seconds: int) -> str:
    """A UTC timestamp ``seconds`` in the past, formatted exactly like
    :func:`utc_now_iso` — this table's timestamps are ISO-8601 UTC text,
    compared and ordered as strings (see the module docstring), so a cutoff
    for that comparison must share its precision and format."""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _queued_in_order():
    """Queued jobs oldest-first — the order a dispatcher should take them in."""
    return (
        select(JobRecord)
        .where(JobRecord.status == JobStatus.QUEUED)
        .order_by(JobRecord.queued_at.asc(), JobRecord.created_at.asc())
    )


def _to_job_dict(row: JobRecord) -> dict[str, Any]:
    """The camelCase job document the rest of the application passes around."""
    return {
        "id": str(row.id),
        "sessionId": str(row.session_id),
        "taskType": str(row.task_type),
        "status": str(row.status),
        "attempts": int(row.attempts or 0),
        "maxAttempts": int(row.max_attempts or 3),
        "createdAt": str(row.created_at or ""),
        "queuedAt": str(row.queued_at) if row.queued_at else None,
        "startedAt": str(row.started_at) if row.started_at else None,
        "endedAt": str(row.ended_at) if row.ended_at else None,
        "requeuedAt": str(row.requeued_at) if row.requeued_at else None,
        "lockedBy": str(row.locked_by) if row.locked_by else None,
        "lockedAt": str(row.locked_at) if row.locked_at else None,
        "updatedAt": str(row.updated_at or ""),
        "error": str(row.error) if row.error else None,
        "payload": _load_json(str(row.payload_json or "{}")),
    }


def _columns_from_job_dict(job: dict[str, Any]) -> dict[str, Any]:
    """Column values for one job, defaulting the fields a caller may omit."""
    updated_at = str(job.get("updatedAt") or "") or utc_now_iso()
    created_at = str(job.get("createdAt") or "") or updated_at
    payload = job.get("payload")
    return {
        "id": str(job["id"]),
        "session_id": str(job["sessionId"]),
        "task_type": str(job["taskType"]),
        "status": str(job["status"]),
        "attempts": int(job.get("attempts") or 0),
        "max_attempts": int(job.get("maxAttempts") or 3),
        "payload_json": _dump_json(payload if isinstance(payload, dict) else {}),
        "error": str(job["error"]) if job.get("error") else None,
        "created_at": created_at,
        "queued_at": str(job["queuedAt"]) if job.get("queuedAt") else None,
        "started_at": str(job["startedAt"]) if job.get("startedAt") else None,
        "ended_at": str(job["endedAt"]) if job.get("endedAt") else None,
        "requeued_at": str(job["requeuedAt"]) if job.get("requeuedAt") else None,
        "locked_by": str(job["lockedBy"]) if job.get("lockedBy") else None,
        "locked_at": str(job["lockedAt"]) if job.get("lockedAt") else None,
        "updated_at": updated_at,
    }


def _dump_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _load_json(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        return {}


__all__ = ["JobClaim", "JobRepository"]
