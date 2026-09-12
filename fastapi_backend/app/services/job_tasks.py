"""The job task-type registry.

Every durable background task is declared here once, with the handler that runs
it and the one policy question the queue has to answer about it: does the queue
drive the session's status, or does the handler?

That flag exists because the tasks are not alike. ``process_session`` and
``auto_crop`` own the session outright — while they run the session is
``processing``, the frontend refuses to open it, and a failure makes the session
terminal. ``export_clips`` is the opposite: the user is sitting in that session's
timeline editor watching clips appear, so flipping the session to ``processing``
would eject them, and a failed export must not bury a session whose clip list is
still perfectly good. Those tasks report progress on their own record instead.

Adding a task type means adding one entry here — the executor has no per-type
branches left to edit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.core.exceptions import AppError
from app.domain.enums import TaskType

# (pipeline, clips, session_id, job_payload) -> None
JobHandler = Callable[[Any, Any, str, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class JobTaskSpec:
    name: TaskType
    run: JobHandler
    # True when the queue mirrors job state onto session.status (queued ->
    # "queued", running -> "processing") and marks the session failed when the
    # job's retries are exhausted. False when the handler owns that itself.
    owns_session_status: bool = True


async def _run_process_session(pipeline: Any, _clips: Any, session_id: str, _payload: dict[str, Any]) -> None:
    await pipeline.process_session_by_id(session_id, allow_processing=True)


async def _run_auto_crop(_pipeline: Any, clips: Any, session_id: str, _payload: dict[str, Any]) -> None:
    await clips.auto_crop_session_by_id(session_id, allow_processing=True)


async def _run_export_clips(_pipeline: Any, clips: Any, session_id: str, payload: dict[str, Any]) -> None:
    await clips.export_clips_by_id(session_id, payload)


JOB_TASK_TYPES: dict[str, JobTaskSpec] = {
    spec.name: spec
    for spec in (
        JobTaskSpec(name=TaskType.PROCESS_SESSION, run=_run_process_session),
        JobTaskSpec(name=TaskType.AUTO_CROP, run=_run_auto_crop),
        JobTaskSpec(name=TaskType.EXPORT_CLIPS, run=_run_export_clips, owns_session_status=False),
    )
}


def get_task_spec(task_type: str) -> JobTaskSpec:
    spec = JOB_TASK_TYPES.get(str(task_type))
    if spec is None:
        raise AppError(f"Unsupported job task type: {task_type}", status_code=500)
    return spec


def queue_owns_session_status(task_type: str) -> bool:
    """Whether the queue may write session.status for this task type.

    Looked up leniently: an unknown type reaching the status-sync path (a job row
    written by an older build, say) keeps the historical behaviour rather than
    silently going unreported.
    """
    spec = JOB_TASK_TYPES.get(str(task_type))
    return True if spec is None else spec.owns_session_status


__all__ = [
    "JOB_TASK_TYPES",
    "JobTaskSpec",
    "get_task_spec",
    "queue_owns_session_status",
]
