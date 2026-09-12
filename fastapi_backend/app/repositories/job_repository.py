from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.json_utils import read_json_file
from app.core.utils import utc_now_iso
from app.database import Database
from app.domain.jobs import ACTIVE_JOB_STATUSES, RECOVERABLE_JOB_STATUSES, JobStatus


@dataclass(frozen=True)
class JobRecord:
    id: str
    session_id: str
    task_type: str
    status: str
    attempts: int = 0
    max_attempts: int = 3
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    created_at: str = ""
    queued_at: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    requeued_at: str | None = None
    locked_by: str | None = None
    locked_at: str | None = None
    updated_at: str = ""

    @classmethod
    def from_job_dict(cls, job: dict[str, Any]) -> "JobRecord":
        return cls(
            id=str(job["id"]),
            session_id=str(job["sessionId"]),
            task_type=str(job["taskType"]),
            status=str(job["status"]),
            attempts=int(job.get("attempts") or 0),
            max_attempts=int(job.get("maxAttempts") or 3),
            payload=job.get("payload") if isinstance(job.get("payload"), dict) else {},
            error=str(job["error"]) if job.get("error") else None,
            created_at=str(job.get("createdAt") or ""),
            queued_at=str(job["queuedAt"]) if job.get("queuedAt") else None,
            started_at=str(job["startedAt"]) if job.get("startedAt") else None,
            ended_at=str(job["endedAt"]) if job.get("endedAt") else None,
            requeued_at=str(job["requeuedAt"]) if job.get("requeuedAt") else None,
            locked_by=str(job["lockedBy"]) if job.get("lockedBy") else None,
            locked_at=str(job["lockedAt"]) if job.get("lockedAt") else None,
            updated_at=str(job.get("updatedAt") or ""),
        )

    def to_job_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sessionId": self.session_id,
            "taskType": self.task_type,
            "status": self.status,
            "attempts": self.attempts,
            "maxAttempts": self.max_attempts,
            "createdAt": self.created_at,
            "queuedAt": self.queued_at,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "requeuedAt": self.requeued_at,
            "lockedBy": self.locked_by,
            "lockedAt": self.locked_at,
            "updatedAt": self.updated_at,
            "error": self.error,
            "payload": self.payload,
        }


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
    def __init__(self, database: Database | Path, legacy_jobs_dir: Path | None = None) -> None:
        if isinstance(database, Database):
            self.database = database
            self.legacy_jobs_dir = legacy_jobs_dir
        else:
            self.database = Database(database.parent / "database" / "osce_marker.sqlite3")
            self.legacy_jobs_dir = database

    async def initialize(self) -> None:
        await self.database.initialize()
        await self.migrate_legacy_jobs()

    async def migrate_legacy_jobs(self) -> int:
        if self.legacy_jobs_dir is None or not self.legacy_jobs_dir.exists():
            return 0

        migrated = 0
        for path in self.legacy_jobs_dir.glob("*.json"):
            payload = await self._read_legacy_job(path)
            if not isinstance(payload, dict) or not payload.get("id"):
                continue
            if await self.exists(str(payload["id"])):
                continue
            await self.write(payload)
            migrated += 1
        return migrated

    async def exists(self, job_id: str) -> bool:
        def _exists(connection: sqlite3.Connection) -> bool:
            row = connection.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return row is not None

        return await self.database.run(_exists)

    async def read(self, job_id: str) -> dict[str, Any]:
        def _read(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise FileNotFoundError(f"Job not found: {job_id}")
            return self._row_to_job(row)

        return await self.database.run(_read)

    async def write(self, job: dict[str, Any]) -> None:
        record = self._coerce_record(job)

        def _write(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, session_id, task_type, status, attempts, max_attempts,
                    payload_json, error, created_at, queued_at, started_at,
                    ended_at, requeued_at, locked_by, locked_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    session_id = excluded.session_id,
                    task_type = excluded.task_type,
                    status = excluded.status,
                    attempts = excluded.attempts,
                    max_attempts = excluded.max_attempts,
                    payload_json = excluded.payload_json,
                    error = excluded.error,
                    created_at = excluded.created_at,
                    queued_at = excluded.queued_at,
                    started_at = excluded.started_at,
                    ended_at = excluded.ended_at,
                    requeued_at = excluded.requeued_at,
                    locked_by = excluded.locked_by,
                    locked_at = excluded.locked_at,
                    updated_at = excluded.updated_at
                """,
                self._record_params(record),
            )

        await self.database.run(_write, write=True)

    async def read_all(self) -> list[dict[str, Any]]:
        def _read_all(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
            return [self._row_to_job(row) for row in rows]

        return await self.database.run(_read_all)

    async def delete_for_session(self, session_id: str) -> int:
        """Delete every job for a session plus its attempt/event rows. Children
        are removed explicitly (SQLite enforces FK cascade only with
        ``PRAGMA foreign_keys=ON``, which this connection does not set)."""

        def _delete(connection: sqlite3.Connection) -> int:
            job_ids = [
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM jobs WHERE session_id = ?", (session_id,)
                ).fetchall()
            ]
            if not job_ids:
                return 0
            placeholders = ",".join("?" for _ in job_ids)
            connection.execute(f"DELETE FROM job_attempts WHERE job_id IN ({placeholders})", job_ids)
            connection.execute(f"DELETE FROM job_events WHERE job_id IN ({placeholders})", job_ids)
            connection.execute("DELETE FROM jobs WHERE session_id = ?", (session_id,))
            return len(job_ids)

        return await self.database.run(_delete, write=True)

    async def list_for_session(self, session_id: str) -> list[dict[str, Any]]:
        def _list(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE session_id = ? ORDER BY created_at DESC",
                (session_id,),
            ).fetchall()
            return [self._row_to_job(row) for row in rows]

        return await self.database.run(_list)

    async def find_active(self, session_id: str, task_type: str) -> dict[str, Any] | None:
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
        parameters = [session_id, task_type, *sorted(ACTIVE_JOB_STATUSES)]

        def _find(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE session_id = ?
                  AND task_type = ?
                  AND status IN ({placeholders})
                ORDER BY created_at DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            return self._row_to_job(row) if row else None

        return await self.database.run(_find)

    async def recover_interrupted_jobs(self) -> list[dict[str, Any]]:
        now = utc_now_iso()

        def _recover(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'queued',
                    queued_at = COALESCE(queued_at, ?),
                    started_at = NULL,
                    ended_at = NULL,
                    requeued_at = ?,
                    locked_by = NULL,
                    locked_at = NULL,
                    updated_at = ?
                WHERE status = 'running'
                """,
                (now, now, now),
            )
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY queued_at ASC, created_at ASC"
            ).fetchall()
            return [self._row_to_job(row) for row in rows]

        return await self.database.run(_recover, write=True)

    async def requeue_interrupted_job(self, job_id: str, reason: str) -> dict[str, Any]:
        now = utc_now_iso()

        def _requeue(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise FileNotFoundError(f"Job not found: {job_id}")
            if str(row["status"]) == JobStatus.RUNNING:
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'queued',
                        queued_at = COALESCE(queued_at, ?),
                        started_at = NULL,
                        ended_at = NULL,
                        requeued_at = ?,
                        locked_by = NULL,
                        locked_at = NULL,
                        error = ?,
                        updated_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (now, now, reason, now, job_id),
                )
                attempt_number = int(row["attempts"] or 0)
                if attempt_number > 0:
                    connection.execute(
                        """
                        UPDATE job_attempts
                        SET status = 'interrupted',
                            ended_at = ?,
                            error = ?
                        WHERE job_id = ?
                          AND attempt_number = ?
                          AND ended_at IS NULL
                        """,
                        (now, reason, job_id, attempt_number),
                    )
            updated = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_job(updated)

        return await self.database.run(_requeue, write=True)

    async def prepare_retry_attempt(self, job_id: str, reason: str) -> dict[str, Any]:
        now = utc_now_iso()

        def _prepare(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise FileNotFoundError(f"Job not found: {job_id}")

            status = str(row["status"])
            attempts = int(row["attempts"] or 0)
            max_attempts = int(row["max_attempts"] or 1)
            if status in RECOVERABLE_JOB_STATUSES and attempts < max_attempts:
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'queued',
                        queued_at = ?,
                        started_at = NULL,
                        ended_at = NULL,
                        requeued_at = ?,
                        locked_by = NULL,
                        locked_at = NULL,
                        error = ?,
                        updated_at = ?
                    WHERE id = ?
                      AND status IN ('failed', 'running')
                      AND attempts < max_attempts
                    """,
                    (now, now, reason, now, job_id),
                )
                if status == JobStatus.RUNNING and attempts > 0:
                    connection.execute(
                        """
                        UPDATE job_attempts
                        SET status = 'interrupted',
                            ended_at = ?,
                            error = ?
                        WHERE job_id = ?
                          AND attempt_number = ?
                          AND ended_at IS NULL
                        """,
                        (now, reason, job_id, attempts),
                    )
            updated = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_job(updated)

        return await self.database.run(_prepare, write=True)

    async def list_queued(self) -> list[dict[str, Any]]:
        def _list(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY queued_at ASC, created_at ASC"
            ).fetchall()
            return [self._row_to_job(row) for row in rows]

        return await self.database.run(_list)

    async def claim_queued(self, job_id: str, worker_id: str) -> JobClaim:
        now = utc_now_iso()

        def _claim(connection: sqlite3.Connection) -> JobClaim:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise FileNotFoundError(f"Job not found: {job_id}")
            if str(row["status"]) != JobStatus.QUEUED:
                return JobClaim("not_queued", self._row_to_job(row))
            if int(row["attempts"] or 0) >= int(row["max_attempts"] or 1):
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'failed',
                        ended_at = ?,
                        error = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, "Maximum retry attempts reached.", now, job_id),
                )
                failed = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                return JobClaim("exhausted", self._row_to_job(failed))

            next_attempt = int(row["attempts"] or 0) + 1
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = 'running',
                    attempts = ?,
                    started_at = ?,
                    ended_at = NULL,
                    error = NULL,
                    locked_by = ?,
                    locked_at = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (next_attempt, now, worker_id, now, now, job_id),
            )
            claimed = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if (
                cursor.rowcount != 1
                or claimed is None
                or str(claimed["status"]) != JobStatus.RUNNING
                or str(claimed["locked_by"] or "") != worker_id
            ):
                return JobClaim("not_queued", self._row_to_job(claimed if claimed is not None else row))
            connection.execute(
                """
                INSERT INTO job_attempts (job_id, attempt_number, worker_id, status, started_at)
                VALUES (?, ?, ?, 'running', ?)
                """,
                (job_id, next_attempt, worker_id, now),
            )
            return JobClaim("claimed", self._row_to_job(claimed))

        return await self.database.run(_claim, write=True)

    async def active_session_ids(self) -> set[str]:
        """Sessions that some job row still intends to run.

        The startup reconciliation compares this against sessions whose status
        claims a job is working on them; a session in that state with no row
        here has nothing left that could ever move it.
        """
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATUSES)

        def _list(connection: sqlite3.Connection) -> set[str]:
            rows = connection.execute(
                f"SELECT DISTINCT session_id FROM jobs WHERE status IN ({placeholders})",
                sorted(ACTIVE_JOB_STATUSES),
            ).fetchall()
            return {str(row["session_id"]) for row in rows}

        return await self.database.run(_list)

    async def mark_succeeded(self, job_id: str) -> dict[str, Any]:
        return await self._finish(job_id, JobStatus.SUCCEEDED, None)

    async def mark_failed(self, job_id: str, error: str) -> dict[str, Any]:
        return await self._finish(job_id, JobStatus.FAILED, error or "Job failed.")

    async def mark_cancelled(self, job_id: str, reason: str) -> dict[str, Any]:
        return await self._finish(job_id, JobStatus.CANCELLED, reason or "Job cancelled.")

    async def rerun(self, job_id: str, *, reset_attempts: bool = True) -> dict[str, Any]:
        now = utc_now_iso()

        def _rerun(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise FileNotFoundError(f"Job not found: {job_id}")
            if str(row["status"]) == JobStatus.RUNNING:
                raise ValueError("Running jobs cannot be rerun until they finish or are cancelled.")
            attempts = 0 if reset_attempts else int(row["attempts"] or 0)
            connection.execute(
                """
                UPDATE jobs
                SET status = 'queued',
                    attempts = ?,
                    queued_at = ?,
                    started_at = NULL,
                    ended_at = NULL,
                    requeued_at = ?,
                    locked_by = NULL,
                    locked_at = NULL,
                    error = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (attempts, now, now, now, job_id),
            )
            updated = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_job(updated)

        return await self.database.run(_rerun, write=True)

    async def append_event(
        self,
        job_id: str,
        event_type: str,
        message: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now_iso()

        def _append(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO job_events (job_id, event_type, message, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, event_type, message, self._dump_json(payload or {}), now),
            )

        await self.database.run(_append, write=True)

    async def _finish(self, job_id: str, status: str, error: str | None) -> dict[str, Any]:
        now = utc_now_iso()

        def _finish_job(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise FileNotFoundError(f"Job not found: {job_id}")
            attempt_number = int(row["attempts"] or 0)
            connection.execute(
                """
                UPDATE jobs
                SET status = ?,
                    ended_at = ?,
                    error = ?,
                    locked_by = NULL,
                    locked_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, now, error, now, job_id),
            )
            if attempt_number > 0:
                connection.execute(
                    """
                    UPDATE job_attempts
                    SET status = ?,
                        ended_at = ?,
                        error = ?
                    WHERE job_id = ?
                      AND attempt_number = ?
                      AND ended_at IS NULL
                    """,
                    (status, now, error, job_id, attempt_number),
                )
            updated = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_job(updated)

        return await self.database.run(_finish_job, write=True)

    async def _read_legacy_job(self, path: Path) -> dict[str, Any] | None:
        return await self.database.run(lambda _connection: read_json_file(path))

    @classmethod
    def _row_to_job(cls, row: sqlite3.Row) -> dict[str, Any]:
        payload = cls._load_json(str(row["payload_json"] or "{}"))
        return JobRecord(
            id=str(row["id"]),
            session_id=str(row["session_id"]),
            task_type=str(row["task_type"]),
            status=str(row["status"]),
            attempts=int(row["attempts"] or 0),
            max_attempts=int(row["max_attempts"] or 3),
            payload=payload,
            error=str(row["error"]) if row["error"] else None,
            created_at=str(row["created_at"]),
            queued_at=str(row["queued_at"]) if row["queued_at"] else None,
            started_at=str(row["started_at"]) if row["started_at"] else None,
            ended_at=str(row["ended_at"]) if row["ended_at"] else None,
            requeued_at=str(row["requeued_at"]) if row["requeued_at"] else None,
            locked_by=str(row["locked_by"]) if row["locked_by"] else None,
            locked_at=str(row["locked_at"]) if row["locked_at"] else None,
            updated_at=str(row["updated_at"]),
        ).to_job_dict()

    @classmethod
    def _coerce_record(cls, job: dict[str, Any]) -> JobRecord:
        now = utc_now_iso()
        payload = dict(job)
        payload.setdefault("createdAt", now)
        payload.setdefault("updatedAt", now)
        payload.setdefault("attempts", 0)
        payload.setdefault("maxAttempts", 3)
        payload.setdefault("payload", {})
        return JobRecord.from_job_dict(payload)

    @classmethod
    def _record_params(cls, record: JobRecord) -> tuple[Any, ...]:
        updated_at = record.updated_at or utc_now_iso()
        created_at = record.created_at or updated_at
        return (
            record.id,
            record.session_id,
            record.task_type,
            record.status,
            record.attempts,
            record.max_attempts,
            cls._dump_json(record.payload),
            record.error,
            created_at,
            record.queued_at,
            record.started_at,
            record.ended_at,
            record.requeued_at,
            record.locked_by,
            record.locked_at,
            updated_at,
        )

    @staticmethod
    def _dump_json(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _load_json(raw: str) -> dict[str, Any]:
        try:
            payload = json.loads(raw)
            return payload if isinstance(payload, dict) else {}
        except json.JSONDecodeError:
            return {}
