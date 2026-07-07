from __future__ import annotations

from typing import Literal


JobStatus = Literal["waiting_for_upload", "queued", "running", "succeeded", "failed", "cancelled"]

ACTIVE_JOB_STATUSES = {"waiting_for_upload", "queued", "running"}
DISPATCHABLE_JOB_STATUSES = {"queued"}
TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled"}
