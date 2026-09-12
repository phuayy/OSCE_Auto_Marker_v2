from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    WAITING_FOR_UPLOAD = "waiting_for_upload"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_JOB_STATUSES = frozenset({JobStatus.WAITING_FOR_UPLOAD, JobStatus.QUEUED, JobStatus.RUNNING})
DISPATCHABLE_JOB_STATUSES = frozenset({JobStatus.QUEUED})
TERMINAL_JOB_STATUSES = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})
IN_PROGRESS_JOB_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.RUNNING})
FINISHED_JOB_STATUSES = frozenset({JobStatus.SUCCEEDED, JobStatus.CANCELLED})
RECOVERABLE_JOB_STATUSES = frozenset({JobStatus.FAILED, JobStatus.RUNNING})
