"""Who may touch a session's clip list: the job-queue half of that question.

``app.domain.sessions.clip_list_busy`` is the pure rule (a job-driven status or
a live export claims the list); it runs inside the ``SessionService.update``
mutator that admits a request, on the freshly read document. A mutator is
synchronous, so the one question that needs the queue — is the export record's
job really still alive? — is answered here first, and its answer travels into
the mutator as the identity of the record that was found dead.
"""

from __future__ import annotations

from typing import Any

from app.domain.enums import TaskType
from app.domain.jobs import ACTIVE_JOB_STATUSES
from app.domain.sessions import active_clip_export, export_identity


async def settled_export_identity(jobs: Any, session: dict[str, Any]) -> tuple[str, str, str] | None:
    """Identity of the session's export record if its job is dead, else ``None``.

    The record is only a claim. A worker killed mid-clip leaves ``running``
    behind, a failed enqueue leaves ``queued`` with no job, and a cancelled job
    never reports back; trusting the record alone would answer 409 forever. The
    job row is the authority: a record with no job that is active — recorded
    or, for one that never got its ``jobId``, found by session and task type —
    is stale and a fresh request may replace it.
    """
    export = active_clip_export(session)
    if export is None:
        return None
    if await _export_job_is_active(jobs, str(session.get("id") or ""), export):
        return None
    return export_identity(export)


async def _export_job_is_active(jobs: Any, session_id: str, export: dict[str, Any]) -> bool:
    job_id = str(export.get("jobId") or "")
    if not job_id:
        return await jobs.repository.find_active(session_id, TaskType.EXPORT_CLIPS) is not None
    try:
        job = await jobs.repository.read(job_id)
    except FileNotFoundError:
        # Only a missing row means the export is gone; a failed read says nothing.
        return False
    return str(job.get("status") or "") in ACTIVE_JOB_STATUSES
