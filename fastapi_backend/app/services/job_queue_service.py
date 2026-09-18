from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.logging_utils import log_context
from app.core.utils import exception_message, parse_iso, utc_now_iso
from app.domain.jobs import (
    ACTIVE_JOB_STATUSES,
    FINISHED_JOB_STATUSES,
    IN_PROGRESS_JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
    JobStatus,
)
from app.domain.session_lifecycle import fail_session
from app.domain.sessions import JOB_DRIVEN_STATUSES, SessionStatus
from app.repositories.job_repository import JobRepository
from app.services.event_service import EventService
from app.services.job_tasks import get_task_spec, queue_owns_session_status
from app.services.session_service import SessionService
from app.storage import ObjectStorage

logger = logging.getLogger(__name__)

# Equal-jitter exponential backoff bounds for retried local job execution, to
# avoid hammering a flapping downstream when many jobs retry at once.
_RETRY_BACKOFF_BASE_SECONDS = 1.0
_RETRY_BACKOFF_CAP_SECONDS = 30.0


def compute_retry_backoff_seconds(
    attempt: int,
    *,
    base: float = _RETRY_BACKOFF_BASE_SECONDS,
    cap: float = _RETRY_BACKOFF_CAP_SECONDS,
) -> float:
    """Equal-jitter exponential backoff for retry ``attempt`` (1-based).

    Returns 0 for the first attempt (no delay) and, for retries, a value in
    ``[ceiling/2, ceiling]`` where ``ceiling = min(cap, base * 2**(attempt-1))``.
    The jitter de-synchronises many simultaneous retries (no thundering herd).
    """
    if attempt <= 1:
        return 0.0
    ceiling = min(cap, base * (2 ** (attempt - 2)))
    half = ceiling / 2
    return round(half + random.uniform(0, half), 3)


def is_retryable_failure(error: BaseException) -> bool:
    """Whether a failed job execution is worth running again.

    An :class:`AppError` below 500 is a deliberate, caller-visible rejection —
    an unsupported task type, a missing source file, a transcript with no
    speech. Re-running it burns the job's remaining attempts and delays the
    error the user needs to see. A few 5xx conditions are permanent for the
    same reason even though the server is at fault (a model the host has no
    memory to load), and say so through ``AppError.retryable``. Everything else
    (subprocess crash, upstream 5xx, transient I/O) is treated as transient and
    retried.
    """
    if isinstance(error, AppError):
        return error.retryable
    return True


@dataclass(frozen=True)
class JobRunResult:
    """Outcome of a single job execution attempt.

    Returned by :meth:`JobQueueService.run_job` so the *scheduler* — not the
    executor — decides whether to run the job again. Keeping that decision out
    of ``_execute_job`` is what lets the local runner retry while the Hatchet
    path (which owns its own retry policy) is left untouched.
    """

    job: dict[str, Any] | None = None
    error: Exception | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None


class JobQueueService:
    def __init__(
        self,
        settings: Settings,
        repository: JobRepository,
        events: EventService,
        sessions: SessionService,
        storage: ObjectStorage,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.events = events
        self.sessions = sessions
        self.storage = storage
        self.pipeline: Any = None
        self.clips: Any = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._worker_id = f"{socket.gethostname()}:{uuid4()}"
        self._semaphore: asyncio.Semaphore | None = None
        # Serialises the find_active -> insert critical section so two concurrent
        # enqueue/create calls for the same session cannot both create a job.
        self._enqueue_lock = asyncio.Lock()

    def bind_handlers(self, *, pipeline: Any, clips: Any) -> None:
        self.pipeline = pipeline
        self.clips = clips

    async def startup(self, *, dispatch_queued: bool = True, recover_interrupted: bool = True) -> None:
        await self.repository.initialize()
        if recover_interrupted:
            queued_jobs = await self.repository.recover_interrupted_jobs()
            # Only the process that owns recovery may declare sessions orphaned:
            # a Hatchet worker booting beside a live API would otherwise fail
            # sessions whose jobs are running in the other process. Runs after
            # recovery (so requeued rows count as active) and before dispatch
            # (so a job that fails on claim finds its session already reconciled).
            await self.reconcile_orphaned_sessions()
        else:
            queued_jobs = await self.repository.list_queued()
        if dispatch_queued:
            for job in queued_jobs:
                await self._dispatch(job)

    async def reconcile_orphaned_sessions(self) -> list[str]:
        """Fail sessions that say a job is working on them when no job row is.

        A session reaches ``queued``/``processing`` in three ways: the queue
        moved it there, an inline request path (``POST /process``) set it and
        died with the process, or a job exhausted its retries in a way that
        never reached ``_fail_session``. In the last two cases nothing will ever
        write the session again — it shows "Processing…" forever, cannot be
        opened, and ``/rerun`` refuses it as in-flight. The job table is the
        authority on "in progress"; a session claiming it without a row is
        terminal, and saying so is what hands it back to the user.
        """
        active = await self.repository.active_session_ids()
        orphaned: list[str] = []
        for entry in await self.sessions.list_sessions():
            status = str(entry.get("status") or "")
            session_id = str(entry.get("id") or "")
            if status not in JOB_DRIVEN_STATUSES or not session_id or session_id in active:
                continue
            orphaned.append(session_id)
            await self._fail_session(
                session_id,
                AppError(
                    "Processing was interrupted by a server restart and no job remains to resume it. "
                    "Re-run the session to score it again.",
                    status_code=503,
                    retryable=False,
                ),
                "Processing was interrupted by a server restart.",
            )
        if orphaned:
            logger.warning(
                "Startup reconciliation: %d session(s) were in flight with no active job and were marked failed: %s",
                len(orphaned),
                ", ".join(orphaned),
            )
        return orphaned

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def create_waiting_job(
        self,
        session_id: str,
        task_type: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self.repository.database.unit_of_work(), self._enqueue_lock:
            existing = await self.repository.find_active(session_id, task_type)
            if existing:
                return existing
            job = {
                "id": str(uuid4()),
                "sessionId": session_id,
                "taskType": task_type,
                "status": JobStatus.WAITING_FOR_UPLOAD,
                "attempts": 0,
                "maxAttempts": self._default_max_attempts(),
                "createdAt": utc_now_iso(),
                "queuedAt": None,
                "startedAt": None,
                "endedAt": None,
                "error": None,
                "payload": payload or {},
            }
            await self.repository.write(job)
        await self.repository.append_event(str(job["id"]), "created", "Waiting for upload.", {"sessionId": session_id})
        return job

    async def enqueue(
        self,
        session_id: str,
        task_type: str,
        payload: dict[str, Any] | None = None,
        *,
        auto_start: bool = True,
    ) -> dict[str, Any]:
        async with self.repository.database.unit_of_work(), self._enqueue_lock:
            existing = await self.repository.find_active(session_id, task_type)
            if existing and str(existing.get("status")) in IN_PROGRESS_JOB_STATUSES:
                return existing

            job = existing or {
                "id": str(uuid4()),
                "sessionId": session_id,
                "taskType": task_type,
                "attempts": 0,
                "maxAttempts": self._default_max_attempts(),
                "createdAt": utc_now_iso(),
                "startedAt": None,
                "endedAt": None,
                "error": None,
                "payload": payload or {},
            }
            job["status"] = JobStatus.QUEUED
            job["queuedAt"] = utc_now_iso()
            job["startedAt"] = None
            job["endedAt"] = None
            job["error"] = None
            if payload is not None:
                job["payload"] = payload
            await self.repository.write(job)
        job_id = str(job["id"])
        logger.info(
            "Job queued: %s (%s) for session %s via %s backend%s.",
            job_id,
            task_type,
            session_id,
            self.settings.job_queue_backend,
            " — dispatching now" if auto_start else " — awaiting start",
            extra=log_context(session_id, "job_enqueue", job_id=job_id, task_type=task_type),
        )
        await self.repository.append_event(job_id, JobStatus.QUEUED, f"{task_type} queued", {"sessionId": session_id})
        await self.events.publish(session_id, "status", {"code": JobStatus.QUEUED, "message": f"{task_type} queued"})
        if auto_start:
            await self._dispatch(job)
        return job

    async def rerun(self, job_id: str) -> dict[str, Any]:
        job = await self.repository.rerun(job_id)
        payload = dict(job.get("payload") or {})
        payload.pop("hatchet", None)
        payload.pop("hatchetRunId", None)
        job["payload"] = payload
        await self.repository.write(job)
        session_id = str(job.get("sessionId"))
        await self.repository.append_event(job_id, "rerun", "Job manually requeued.", {"sessionId": session_id})
        await self._sync_session_job(job)
        await self.events.publish(
            session_id,
            "status",
            {"code": JobStatus.QUEUED, "message": f"{job.get('taskType')} requeued"},
        )
        await self._dispatch(job)
        return job

    async def list_jobs(self, session_id: str | None = None) -> list[dict[str, Any]]:
        if session_id:
            return await self.repository.list_for_session(session_id)
        return await self.repository.read_all()

    async def read(self, job_id: str) -> dict[str, Any]:
        return await self.repository.read(job_id)

    async def start_job(self, job: dict[str, Any]) -> None:
        await self._dispatch(job)

    async def cancel(self, job_id: str, reason: str = JobStatus.CANCELLED) -> dict[str, Any]:
        job = await self.repository.read(job_id)
        if str(job.get("status")) in TERMINAL_JOB_STATUSES:
            return job
        job = await self.repository.mark_cancelled(job_id, reason)
        task = self._tasks.pop(str(job_id), None)
        if task:
            task.cancel()
        await self._cancel_hatchet_run(job)
        await self.repository.append_event(job_id, JobStatus.CANCELLED, reason, {"sessionId": job.get("sessionId")})
        await self.events.publish(str(job.get("sessionId")), "status", {"code": JobStatus.CANCELLED, "message": reason})
        await self._sync_session_job(job)
        return job

    async def _cancel_hatchet_run(self, job: dict[str, Any]) -> None:
        """Best-effort engine-side abort of a job's dispatched Hatchet run.

        Marking the row ``cancelled`` (or deleting it in ``purge_session``)
        only stops the *local* bookkeeping — it never told the Hatchet engine
        the step run it dispatched should stop. Left alone, that run keeps
        executing or retrying with no DB row to report back to: a retry
        eventually calls ``prepare_hatchet_retry_attempt`` for a job id that no
        longer exists. This is a best-effort abort — a transport error here is
        logged and recorded, never raised, because the row is already
        cancelled and the caller (often a session purge) must proceed either
        way; the other half of this race is that ``process_job`` treats a
        missing job row as "already gone" rather than crashing.
        """
        if self.settings.job_queue_backend != "hatchet":
            return
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        hatchet_meta = payload.get("hatchet") if isinstance(payload.get("hatchet"), dict) else {}
        run_id = hatchet_meta.get("runId") or payload.get("hatchetRunId")
        if not run_id:
            return
        job_id = str(job.get("id"))
        try:
            from app.queue.hatchet_tasks import hatchet

            await hatchet.runs.aio_cancel(str(run_id))
            logger.info("Cancelled Hatchet run %s for job %s.", run_id, job_id)
        except Exception as error:
            logger.warning(
                "Failed to cancel Hatchet run %s for job %s: %s. The engine-side run may keep executing.",
                run_id,
                job_id,
                error,
            )
            await self.repository.append_event(
                job_id,
                "hatchet_cancel_failed",
                f"Could not cancel Hatchet run {run_id}: {error}",
                {"sessionId": job.get("sessionId"), "hatchetRunId": run_id},
            )

    async def purge_session(self, session_id: str) -> int:
        """Cancel any in-flight job for a session and delete all of its job
        rows. Cancel first so the local worker task stops touching the session
        that is about to be deleted."""
        jobs = await self.repository.list_for_session(session_id)
        for job in jobs:
            job_id = str(job.get("id"))
            if str(job.get("status")) in ACTIVE_JOB_STATUSES:
                try:
                    await self.cancel(job_id, "Session deleted.")
                except Exception:
                    logger.warning(
                        "Failed to cancel job %s while purging session %s.",
                        job_id,
                        session_id,
                        extra=log_context(session_id, "session_purge"),
                    )
            task = self._tasks.pop(job_id, None)
            if task:
                task.cancel()
        return await self.repository.delete_for_session(session_id)

    def public_job(self, job: dict[str, Any] | None) -> dict[str, Any] | None:
        if not job:
            return None
        return {
            "id": job.get("id"),
            "sessionId": job.get("sessionId"),
            "taskType": job.get("taskType"),
            "status": job.get("status"),
            "attempts": job.get("attempts"),
            "maxAttempts": job.get("maxAttempts"),
            "createdAt": job.get("createdAt"),
            "queuedAt": job.get("queuedAt"),
            "startedAt": job.get("startedAt"),
            "endedAt": job.get("endedAt"),
            "requeuedAt": job.get("requeuedAt"),
            "error": job.get("error"),
        }

    def _default_max_attempts(self) -> int:
        if self.settings.job_queue_backend == "hatchet":
            return max(1, self.settings.hatchet_job_retries + 1)
        return 3

    async def run_job(self, job_id: str, *, raise_on_error: bool = False) -> JobRunResult:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(max(1, self.settings.job_worker_concurrency))
        async with self._semaphore:
            return await self._execute_job(job_id, raise_on_error=raise_on_error)

    async def prepare_hatchet_retry_attempt(self, job_id: str, retry_count: int) -> bool:
        """Ready a job row for its next Hatchet-driven attempt.

        Returns ``False`` when the row is gone — the session was deleted, or
        ``purge_session``/``rerun`` cleared it — out from under a run Hatchet
        already had in flight. A cancel attempts to abort that run engine-side
        (see ``_cancel_hatchet_run``), but the abort is best-effort and a
        scheduled retry can still land after the row is deleted. That used to
        surface as an unhandled ``FileNotFoundError`` crashing the Hatchet
        task; the caller (``process_job``) now reads ``False`` as "nothing left
        to do" and stops without running the job or raising.
        """
        if retry_count <= 0:
            return True
        try:
            job = await self.repository.prepare_retry_attempt(
                job_id,
                f"Preparing Hatchet retry attempt {retry_count}.",
            )
        except FileNotFoundError:
            logger.info(
                "Job %s no longer exists; skipping Hatchet retry attempt %d.",
                job_id,
                retry_count,
            )
            return False
        await self.repository.append_event(
            job_id,
            "retry_prepared",
            f"Prepared Hatchet retry attempt {retry_count}.",
            {"sessionId": job.get("sessionId"), "retryCount": retry_count},
        )
        await self._sync_session_job(job)
        return True

    async def redispatch_stale_hatchet_jobs(self) -> None:
        """Dispatch queued jobs that Hatchet never received, and re-dispatch ones
        whose dispatch went stale.

        Runs both at worker startup and on an interval (see the worker lifespan),
        so that a job which failed to dispatch at enqueue time (e.g. the API
        process's Hatchet client was unavailable) is still picked up continuously
        — not only after a restart.

        Two cases are handled, both scoped to ``queued`` jobs only:
          * **undispatched** — no Hatchet metadata at all (never dispatched, or
            metadata cleared after a server-side failure).
          * **stale** — ``dispatchedAt`` older than
            ``hatchet_job_schedule_timeout_minutes``; the metadata is cleared so
            the idempotency guard in ``_dispatch_hatchet`` does not skip it.

        Both are safe to repeat: ``_dispatch_hatchet`` skips jobs that already
        carry fresh metadata, and ``claim_queued`` atomically no-ops a job that a
        worker has meanwhile started, so a duplicate dispatch cannot double-run.
        """
        if self.settings.job_queue_backend != "hatchet":
            return

        timeout_minutes = self.settings.hatchet_job_schedule_timeout_minutes
        stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)

        # Only queued jobs can be (re)dispatched — avoids scanning the full job
        # history on every interval.
        queued_jobs = await self.repository.list_queued()
        stale: list[dict[str, Any]] = []
        for job in queued_jobs:
            payload = job.get("payload") or {}
            hatchet_meta = payload.get("hatchet") if isinstance(payload.get("hatchet"), dict) else {}
            dispatched_at_str = hatchet_meta.get("dispatchedAt")
            if not dispatched_at_str:
                continue
            dispatched_at = parse_iso(dispatched_at_str)
            if dispatched_at is not None and dispatched_at < stale_cutoff:
                stale.append(job)

        undispatched = [j for j in queued_jobs if not (j.get("payload") or {}).get("hatchet")]
        if undispatched:
            logger.info(
                "Hatchet recovery: dispatching %d queued job(s) with no Hatchet metadata.",
                len(undispatched),
            )
            for job in undispatched:
                await self._dispatch(job)
                logger.info("Dispatched pending job %s to Hatchet.", str(job["id"]))

        if not stale:
            return

        logger.warning(
            "Hatchet recovery: %d queued job(s) have stale Hatchet dispatches "
            "(older than %d minutes). Re-dispatching.",
            len(stale),
            timeout_minutes,
        )

        for job in stale:
            job_id = str(job["id"])
            payload = dict(job.get("payload") or {})
            old_run_id = (payload.get("hatchet") or {}).get("runId")
            payload.pop("hatchet", None)
            job["payload"] = payload
            await self.repository.write(job)
            await self.repository.append_event(
                job_id,
                "redispatch_stale",
                f"Stale Hatchet dispatch (runId={old_run_id}) cleared. Re-dispatching.",
            )
            await self._dispatch(job)
            logger.info("Stale job %s re-dispatched to Hatchet.", job_id)

    async def _dispatch(self, job: dict[str, Any]) -> None:
        backend = self.settings.job_queue_backend
        if backend == "local":
            self._schedule_local(job)
            return
        if backend == "hatchet":
            await self._dispatch_hatchet(job)
            return
        await self.repository.append_event(
            str(job["id"]),
            "dispatch_skipped",
            f"Unsupported JOB_QUEUE_BACKEND={backend}; job remains queued.",
        )

    def _schedule_local(self, job: dict[str, Any]) -> None:
        if not self.settings.local_job_auto_start:
            return
        if str(job.get("status")) != JobStatus.QUEUED:
            return
        job_id = str(job["id"])
        if job_id in self._tasks and not self._tasks[job_id].done():
            return
        upcoming_attempt = int(job.get("attempts") or 0) + 1
        try:
            self._tasks[job_id] = asyncio.create_task(self._run_local(job_id, upcoming_attempt))
        except RuntimeError:
            return

    async def _dispatch_hatchet(self, job: dict[str, Any]) -> None:
        job_id = str(job["id"])
        payload = dict(job.get("payload") or {})
        hatchet_meta = payload.get("hatchet") if isinstance(payload.get("hatchet"), dict) else {}
        if hatchet_meta.get("dispatchedAt") or hatchet_meta.get("runId") or payload.get("hatchetRunId"):
            await self.repository.append_event(
                job_id,
                "dispatch_skipped",
                "Queued job already has Hatchet dispatch metadata; skipping duplicate dispatch.",
                {"hatchet": hatchet_meta or {"runId": payload.get("hatchetRunId")}},
            )
            return
        try:
            from app.queue.hatchet_tasks import enqueue_process_job

            run_ref = await enqueue_process_job(job_id)
            run_id = self._hatchet_run_id(run_ref)
            payload["hatchet"] = {
                "runId": run_id,
                "dispatchedAt": utc_now_iso(),
                "task": "osce-process-job",
            }
            job["payload"] = payload
            await self.repository.write(job)
            await self.repository.append_event(
                job_id,
                "dispatched",
                "Job dispatched to Hatchet.",
                {"hatchetRunId": run_id},
            )
            logger.info("Job %s dispatched to Hatchet (runId=%s).", job_id, run_id)
        except Exception as error:
            logger.error(
                "Failed to dispatch job %s to Hatchet: %s — job remains queued.",
                job_id,
                error,
            )
            await self.repository.append_event(
                job_id, "dispatch_failed", str(error) or "Hatchet dispatch failed."
            )
            await self.events.publish(
                str(job.get("sessionId")),
                "status",
                {
                    "code": JobStatus.QUEUED,
                    "message": "Job is queued but could not be dispatched to Hatchet.",
                },
            )

    @staticmethod
    def _hatchet_run_id(run_ref: Any) -> str | None:
        for attribute in ("workflow_run_id", "workflowRunId", "run_id", "runId", "id"):
            value = getattr(run_ref, attribute, None)
            if value:
                return str(value)
        return None

    async def _run_local(self, job_id: str, attempt: int = 1) -> None:
        """Execute a job in-process, retrying transient failures.

        The retry loop lives in the runner rather than in ``_execute_job``
        because re-running a job is a scheduling concern: Hatchet applies its
        own retry policy, so only the local backend re-runs here. Looping also
        sidesteps the ``_tasks`` in-flight guard in ``_schedule_local``, which
        would silently drop a re-dispatch issued from inside this very task.
        """
        try:
            current_attempt = attempt
            while True:
                backoff_seconds = compute_retry_backoff_seconds(current_attempt)
                if backoff_seconds > 0:
                    logger.info(
                        "Delaying local retry of job %s by %.3fs.",
                        job_id,
                        backoff_seconds,
                        extra=log_context(job_id, "job_retry_backoff", attempt=current_attempt),
                    )
                    await asyncio.sleep(backoff_seconds)
                try:
                    result = await self.run_job(job_id)
                except Exception:
                    # _execute_job already turns every failure it recognises
                    # into a JobRunResult; reaching here means something raised
                    # from outside that path entirely (e.g. a DB error inside
                    # claim_queued itself). Logged so this is not only visible
                    # as asyncio's "Task exception was never retrieved" —
                    # recovery still falls to the periodic stale-job reaper,
                    # since a claimed-but-abandoned row's heartbeat has simply
                    # stopped, the same signal a genuine worker death leaves.
                    logger.exception(
                        "Unhandled error running job %s; leaving it to the stale-job reaper.",
                        job_id,
                        extra=log_context(job_id, "job_run_unhandled_error"),
                    )
                    return
                if not await self._arm_local_retry(result):
                    return
                current_attempt += 1
        finally:
            self._tasks.pop(job_id, None)

    def _local_retry_available(self, job: dict[str, Any], error: Exception) -> bool:
        """Whether the local runner will re-run this just-failed job.

        A pure predicate with no side effects: ``_execute_job`` consults it to
        decide whether to leave the session in a terminal ``failed`` state, and
        ``_arm_local_retry`` re-checks it before performing the transition.
        """
        if self.settings.job_queue_backend != "local":
            return False
        if not self.settings.local_job_auto_start:
            return False
        if not is_retryable_failure(error):
            return False
        return int(job.get("attempts") or 0) < int(job.get("maxAttempts") or 1)

    async def _arm_local_retry(self, result: JobRunResult) -> bool:
        """Return a failed job to ``queued`` so the runner can execute it again.

        Returns True only when the repository actually moved the row back to
        ``queued``. Anything else is terminal, and the session — which
        ``_execute_job`` deliberately left non-terminal in anticipation of a
        retry — is failed here so it can never be stranded mid-flight.
        """
        job = result.job
        error = result.error
        if job is None or error is None:
            return False

        job_id = str(job["id"])
        session_id = str(job.get("sessionId"))
        task_type = str(job.get("taskType"))
        attempts = int(job.get("attempts") or 0)
        max_attempts = int(job.get("maxAttempts") or 1)
        message = self._exception_message(error, "Job failed.")

        if not self._local_retry_available(job, error):
            reason = (
                f"Permanent failure ({message}); not retrying."
                if not is_retryable_failure(error)
                else f"Retries exhausted after {attempts}/{max_attempts} attempt(s): {message}"
            )
            await self.repository.append_event(job_id, "retry_skipped", reason, {"sessionId": session_id})
            return False

        requeued = await self.repository.prepare_retry_attempt(
            job_id,
            f"Retrying after failure: {message}",
        )
        if str(requeued.get("status")) != JobStatus.QUEUED:
            # The row moved on (cancelled, or attempts exhausted concurrently);
            # honour that and finish failing the session.
            await self._fail_session(session_id, error, message)
            return False

        next_attempt = attempts + 1
        logger.info(
            "Retrying job %s locally (attempt %d/%d) after failure: %s",
            job_id,
            next_attempt,
            max_attempts,
            message,
            extra=log_context(session_id, "job_retry", job_id=job_id, task_type=task_type, attempt=next_attempt),
        )
        await self.repository.append_event(
            job_id,
            "retry_scheduled",
            f"Attempt {next_attempt}/{max_attempts} scheduled after failure: {message}",
            {"sessionId": session_id, "attempt": next_attempt},
        )
        await self._sync_session_job(requeued)
        await self.events.publish(
            session_id,
            "status",
            {
                "code": JobStatus.QUEUED,
                "message": f"{task_type} failed; retry {next_attempt}/{max_attempts} queued.",
            },
        )
        return True

    async def _execute_job(self, job_id: str, *, raise_on_error: bool = False) -> JobRunResult:
        try:
            claim = await self.repository.claim_queued(job_id, self._worker_id)
        except FileNotFoundError:
            # Row was purged (session deleted, or rerun cleared it) between
            # dispatch and claim. Nothing left to run, and nothing to raise —
            # a purge already cancels the Hatchet-side run on a best-effort
            # basis, but a scheduled retry can still slip in after the delete.
            logger.info("Job %s no longer exists; nothing to execute.", job_id)
            return JobRunResult()
        if claim.exhausted:
            # The row just went terminal without ever running. The session it
            # belongs to is still waiting on it and nothing else will ever touch
            # it, so this is the moment it has to be failed — otherwise it is a
            # zombie: un-openable, un-rerunnable, only deletable.
            failed = claim.job
            message = str(failed.get("error") or "Maximum retry attempts reached.")
            exhausted_session = str(failed.get("sessionId"))
            exhausted_type = str(failed.get("taskType") or "")
            await self.repository.append_event(
                job_id, "exhausted", message, {"sessionId": exhausted_session, "attempts": failed.get("attempts")}
            )
            await self._sync_session_job(failed)
            error = AppError(message, status_code=500, retryable=False)
            if queue_owns_session_status(exhausted_type):
                await self._fail_session(exhausted_session, error, message)
            if raise_on_error:
                raise error
            return JobRunResult(job=failed, error=error)
        if not claim.claimed:
            if raise_on_error:
                status = str(claim.job.get("status") or "")
                if status in FINISHED_JOB_STATUSES:
                    return JobRunResult()
                raise RuntimeError(f"Job {job_id} could not be claimed for execution (status={status or 'unknown'}).")
            return JobRunResult()
        job = claim.job

        session_id = str(job.get("sessionId"))
        task_type = str(job.get("taskType"))
        job_context = log_context(session_id, "job_execution", job_id=job_id, task_type=task_type)
        # Refreshes jobs.locked_at while this job is genuinely still running,
        # so the periodic reaper can tell "still working, just slow" apart
        # from "the task that claimed this died" (see _heartbeat_loop).
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(job_id, self._worker_id))
        try:
            if self.pipeline is None or self.clips is None:
                raise RuntimeError("Job handlers are not bound.")

            logger.info("Job running: %s", task_type, extra=job_context)
            await self.repository.append_event(job_id, JobStatus.RUNNING, f"{task_type} running", {"workerId": self._worker_id})
            await self._sync_session_job(job)
            await self.events.publish(session_id, "status", {"code": JobStatus.RUNNING, "message": f"{task_type} running"})

            # Source fixup (a path rewrite on local storage, a checksum-verified
            # download on a bucket) is applied through ``update`` so the write
            # cannot clobber a rename or a status the API committed meanwhile.
            prepared = await self.storage.prepare_session_sources(await self.sessions.read(session_id))
            prepared_files = prepared.get("files")

            def adopt_sources(current: dict[str, Any]) -> Any:
                if current.get("files") == prepared_files:
                    return False
                current["files"] = prepared_files
                return None

            await self.sessions.update(session_id, adopt_sources)

            spec = get_task_spec(task_type)
            payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
            await spec.run(self.pipeline, self.clips, session_id, payload)

            finished = await self.repository.mark_succeeded(job_id)
            logger.info("Job succeeded: %s", task_type, extra=job_context)
            await self.repository.append_event(job_id, JobStatus.SUCCEEDED, f"{task_type} succeeded", {"sessionId": session_id})
            await self._sync_session_job(finished)
            await self.events.publish(session_id, "status", {"code": JobStatus.SUCCEEDED, "message": f"{task_type} succeeded"})
            return JobRunResult(job=finished)
        except asyncio.CancelledError:
            requeued = await self.repository.requeue_interrupted_job(
                job_id,
                "Worker task was cancelled before completion.",
            )
            if requeued.get("status") == JobStatus.QUEUED:
                await self.repository.append_event(
                    job_id,
                    "interrupted",
                    "Worker task was cancelled; job returned to queued state.",
                    {"sessionId": session_id},
                )
                await self._sync_session_job(requeued)
                await self.events.publish(
                    session_id,
                    "status",
                    {"code": JobStatus.QUEUED, "message": f"{task_type} was interrupted and requeued."},
                )
            raise
        except Exception as error:
            message = self._exception_message(error, "Job failed.")
            logger.error("Job failed: %s (%s)", task_type, message, extra=job_context)
            failed = await self.repository.mark_failed(job_id, message)
            await self.repository.append_event(job_id, JobStatus.FAILED, message, {"sessionId": session_id})
            await self._sync_session_job(failed)
            # When the local runner will re-run this job, the session must stay
            # non-terminal — marking it failed here would surface a dead session
            # to the user while a retry is still pending. ``_arm_local_retry``
            # fails the session itself if the requeue does not take.
            if (raise_on_error or not self._local_retry_available(failed, error)) and queue_owns_session_status(
                task_type
            ):
                await self._fail_session(session_id, error, message)
            if raise_on_error:
                raise
            return JobRunResult(job=failed, error=error)
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

    async def _heartbeat_loop(self, job_id: str, worker_id: str) -> None:
        """Keep ``jobs.locked_at`` fresh for as long as this worker is
        actually executing ``job_id``.

        Cancelled from ``_execute_job``'s ``finally`` the moment the job
        finishes, fails, or this task is itself cancelled — so a stale
        ``locked_at`` can only mean the heartbeat stopped being renewed,
        which happens only when nothing is running this job any more.
        A DB hiccup here is not this loop's problem to solve: it is logged
        and retried on the next tick, and the periodic reaper is the
        backstop if the database stays unreachable long enough for the
        heartbeat to actually go stale.
        """
        while True:
            await asyncio.sleep(self.settings.job_heartbeat_interval_seconds)
            try:
                await self.repository.touch_heartbeat(job_id, worker_id)
            except Exception:
                logger.warning(
                    "Heartbeat failed for job %s; the stale-job reaper will recover it if this "
                    "persists.",
                    job_id,
                    exc_info=True,
                )

    async def reap_stale_jobs(self) -> list[str]:
        """Requeue every ``running`` job whose heartbeat has gone stale.

        A stale heartbeat (see :meth:`_heartbeat_loop`) is the signal that
        whatever claimed this job is no longer executing it — an unhandled
        exception outside the paths ``_execute_job`` recognises (a DB error
        inside ``claim_queued`` itself, or inside ``mark_failed``), or the
        process that claimed it is simply gone. Without this, such a job sits
        at ``running`` forever: ``recover_interrupted_jobs`` only runs at API
        startup on the ``local`` backend, and nothing today revisits a
        Hatchet-backend job stuck this way at all.

        Reuses ``requeue_interrupted_job`` — the same primitive startup
        recovery and a cancelled task both already use — then, regardless of
        backend, clears any stale Hatchet dispatch metadata and redispatches
        immediately, the same pop-then-dispatch sequence :meth:`rerun` and
        the stale-dispatch branch of :meth:`redispatch_stale_hatchet_jobs`
        already perform. Dispatching immediately (rather than leaving that
        loop to notice on its own schedule) matters because its own staleness
        check measures time since *dispatch*, not since the heartbeat went
        quiet, and could otherwise sit on an already-orphaned row for up to
        ``hatchet_job_schedule_timeout_minutes``.
        """
        stale = await self.repository.list_stale_running(self.settings.job_stale_running_timeout_seconds)
        reaped: list[str] = []
        for job in stale:
            job_id = str(job["id"])
            session_id = str(job.get("sessionId"))
            task_type = str(job.get("taskType"))
            reason = (
                f"No heartbeat for over {self.settings.job_stale_running_timeout_seconds}s; "
                "assuming the worker that claimed this job is gone."
            )
            requeued = await self.repository.requeue_interrupted_job(job_id, reason)
            if requeued.get("status") != JobStatus.QUEUED:
                # Finished, cancelled, or reclaimed by a live worker between
                # the scan and this requeue attempt — nothing to reap.
                continue
            payload = dict(requeued.get("payload") or {})
            if payload.pop("hatchet", None) is not None:
                requeued["payload"] = payload
                await self.repository.write(requeued)
            await self.repository.append_event(
                job_id,
                "reaped_stale",
                reason,
                {"sessionId": session_id},
            )
            await self._sync_session_job(requeued)
            await self.events.publish(
                session_id,
                "status",
                {"code": JobStatus.QUEUED, "message": f"{task_type} was orphaned and has been requeued."},
            )
            logger.warning(
                "Reaped stale job %s (session %s): %s",
                job_id,
                session_id,
                reason,
                extra=log_context(session_id, "job_reaped_stale", job_id=job_id, task_type=task_type),
            )
            await self._dispatch(requeued)
            reaped.append(job_id)
        return reaped

    async def stale_job_reaper_loop(self) -> None:
        """Periodic companion to the startup-only recovery paths above.

        Template: ``hatchet_worker._redispatch_loop`` — sleep first, catch
        and log per iteration rather than let one bad scan end the loop,
        no ``CancelledError`` handler so shutdown (``BackgroundTaskRegistry
        .cancel_all()``) cancels it cleanly out of ``asyncio.sleep``.
        """
        interval = self.settings.job_reaper_interval_seconds
        while True:
            await asyncio.sleep(interval)
            try:
                await self.reap_stale_jobs()
            except Exception:
                logger.exception("Stale-job reaper scan failed; will retry on the next interval.")

    async def _fail_session(self, session_id: str, error: Exception, message: str) -> None:
        """Put a session into its terminal failed state for a failed job."""
        if self.pipeline is not None:
            await self.pipeline.mark_session_failed(session_id, error)
            return
        await self.sessions.update(session_id, lambda session: fail_session(session, message))
        await self.events.publish(session_id, "status", {"code": JobStatus.FAILED, "message": message})

    async def _sync_session_job(self, job: dict[str, Any]) -> None:
        """Mirror the job row onto its session — through ``update``, because the
        pipeline in this or another process may be writing the same document."""
        session_id = str(job.get("sessionId"))
        public_job = self.public_job(job)
        owns_status = queue_owns_session_status(str(job.get("taskType") or ""))
        job_status = str(job.get("status") or "")

        def mutate(session: dict[str, Any]) -> Any:
            session["job"] = public_job
            if not owns_status:
                # The handler reports its own progress (see app.services.job_tasks).
                # Overwriting status here would eject a user from a session they
                # are actively working in.
                return None
            if job_status == JobStatus.RUNNING:
                session["status"] = SessionStatus.PROCESSING
                session["error"] = None
            elif job_status == JobStatus.QUEUED and session.get("status") != SessionStatus.COMPLETED:
                session["status"] = SessionStatus.QUEUED
                session["error"] = None
            return None

        try:
            await self.sessions.update(session_id, mutate)
        except FileNotFoundError:
            return
        except Exception:
            logger.warning(
                "Could not mirror job %s onto session %s.",
                job.get("id"),
                session_id,
                exc_info=True,
                extra=log_context(session_id, "job_session_sync"),
            )

    _exception_message = staticmethod(exception_message)
