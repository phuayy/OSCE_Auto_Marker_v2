from __future__ import annotations

import asyncio
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
from app.core.utils import utc_now_iso
from app.domain.jobs import ACTIVE_JOB_STATUSES
from app.repositories.job_repository import JobRepository
from app.services.event_service import EventService
from app.services.session_service import SessionService
from app.services.job_tasks import get_task_spec, queue_owns_session_status
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
    error the user needs to see. Everything else (subprocess crash, upstream
    5xx, GPU OOM, transient I/O) is treated as transient and retried.
    """
    if isinstance(error, AppError):
        return error.status_code >= 500
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
        else:
            queued_jobs = await self.repository.list_queued()
        if dispatch_queued:
            for job in queued_jobs:
                await self._dispatch(job)

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
        async with self._enqueue_lock:
            existing = await self.repository.find_active(session_id, task_type)
            if existing:
                return existing
            job = {
                "id": str(uuid4()),
                "sessionId": session_id,
                "taskType": task_type,
                "status": "waiting_for_upload",
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
        async with self._enqueue_lock:
            existing = await self.repository.find_active(session_id, task_type)
            if existing and str(existing.get("status")) in {"queued", "running"}:
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
            job["status"] = "queued"
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
        await self.repository.append_event(job_id, "queued", f"{task_type} queued", {"sessionId": session_id})
        await self.events.publish(session_id, "status", {"code": "queued", "message": f"{task_type} queued"})
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
            {"code": "queued", "message": f"{job.get('taskType')} requeued"},
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

    async def cancel(self, job_id: str, reason: str = "cancelled") -> dict[str, Any]:
        job = await self.repository.read(job_id)
        if str(job.get("status")) in {"succeeded", "failed", "cancelled"}:
            return job
        job = await self.repository.mark_cancelled(job_id, reason)
        task = self._tasks.pop(str(job_id), None)
        if task:
            task.cancel()
        await self.repository.append_event(job_id, "cancelled", reason, {"sessionId": job.get("sessionId")})
        await self.events.publish(str(job.get("sessionId")), "status", {"code": "cancelled", "message": reason})
        await self._sync_session_job(job)
        return job

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

    async def prepare_hatchet_retry_attempt(self, job_id: str, retry_count: int) -> None:
        if retry_count <= 0:
            return
        job = await self.repository.prepare_retry_attempt(
            job_id,
            f"Preparing Hatchet retry attempt {retry_count}.",
        )
        await self.repository.append_event(
            job_id,
            "retry_prepared",
            f"Prepared Hatchet retry attempt {retry_count}.",
            {"sessionId": job.get("sessionId"), "retryCount": retry_count},
        )
        await self._sync_session_job(job)

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
            try:
                dispatched_at = datetime.fromisoformat(
                    str(dispatched_at_str).replace("Z", "+00:00")
                )
                if dispatched_at < stale_cutoff:
                    stale.append(job)
            except (ValueError, TypeError):
                continue

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
        if str(job.get("status")) != "queued":
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
                    "code": "queued",
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
                result = await self.run_job(job_id)
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
        if str(requeued.get("status")) != "queued":
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
                "code": "queued",
                "message": f"{task_type} failed; retry {next_attempt}/{max_attempts} queued.",
            },
        )
        return True

    async def _execute_job(self, job_id: str, *, raise_on_error: bool = False) -> JobRunResult:
        job = await self.repository.claim_queued(job_id, self._worker_id)
        if job is None:
            if raise_on_error:
                current = await self.repository.read(job_id)
                status = str(current.get("status") or "")
                if status in {"succeeded", "cancelled"}:
                    return JobRunResult()
                raise RuntimeError(f"Job {job_id} could not be claimed for execution (status={status or 'unknown'}).")
            return JobRunResult()

        session_id = str(job.get("sessionId"))
        task_type = str(job.get("taskType"))
        job_context = log_context(session_id, "job_execution", job_id=job_id, task_type=task_type)
        try:
            if self.pipeline is None or self.clips is None:
                raise RuntimeError("Job handlers are not bound.")

            logger.info("Job running: %s", task_type, extra=job_context)
            await self.repository.append_event(job_id, "running", f"{task_type} running", {"workerId": self._worker_id})
            await self._sync_session_job(job)
            await self.events.publish(session_id, "status", {"code": "running", "message": f"{task_type} running"})

            session = await self.sessions.read(session_id)
            session = await self.storage.prepare_session_sources(session)
            await self.sessions.write(session)

            spec = get_task_spec(task_type)
            payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
            await spec.run(self.pipeline, self.clips, session_id, payload)

            finished = await self.repository.mark_succeeded(job_id)
            logger.info("Job succeeded: %s", task_type, extra=job_context)
            await self.repository.append_event(job_id, "succeeded", f"{task_type} succeeded", {"sessionId": session_id})
            await self._sync_session_job(finished)
            await self.events.publish(session_id, "status", {"code": "succeeded", "message": f"{task_type} succeeded"})
            return JobRunResult(job=finished)
        except asyncio.CancelledError:
            requeued = await self.repository.requeue_interrupted_job(
                job_id,
                "Worker task was cancelled before completion.",
            )
            if requeued.get("status") == "queued":
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
                    {"code": "queued", "message": f"{task_type} was interrupted and requeued."},
                )
            raise
        except Exception as error:
            message = self._exception_message(error, "Job failed.")
            logger.error("Job failed: %s (%s)", task_type, message, extra=job_context)
            failed = await self.repository.mark_failed(job_id, message)
            await self.repository.append_event(job_id, "failed", message, {"sessionId": session_id})
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

    async def _fail_session(self, session_id: str, error: Exception, message: str) -> None:
        """Put a session into its terminal failed state for a failed job."""
        if self.pipeline is not None:
            await self.pipeline.mark_session_failed(session_id, error)
            return
        await self.events.publish(session_id, "status", {"code": "failed", "message": message})

    async def _sync_session_job(self, job: dict[str, Any]) -> None:
        try:
            session = await self.sessions.read(str(job.get("sessionId")))
        except Exception:
            return
        session["job"] = self.public_job(job)
        if not queue_owns_session_status(str(job.get("taskType") or "")):
            # The handler reports its own progress (see app.services.job_tasks).
            # Overwriting status here would eject a user from a session they are
            # actively working in.
            await self.sessions.write(session)
            return
        job_status = str(job.get("status") or "")
        if job_status == "running":
            session["status"] = "processing"
            session["error"] = None
        elif job_status == "queued" and session.get("status") != "completed":
            session["status"] = "queued"
            session["error"] = None
        await self.sessions.write(session)

    @staticmethod
    def _exception_message(error: Exception, fallback: str) -> str:
        return str(error) or type(error).__name__ or fallback
