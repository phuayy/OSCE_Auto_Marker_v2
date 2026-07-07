from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
