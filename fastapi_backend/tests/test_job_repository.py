from __future__ import annotations

import asyncio

from app.database import Database
from app.repositories.job_repository import JobRepository


def test_job_repository_recovers_running_jobs(tmp_path) -> None:
    repository = JobRepository(Database(tmp_path / "osce_marker.sqlite3"))
    job = {
        "id": "job-1",
        "sessionId": "session-1",
        "taskType": "process_session",
        "status": "running",
        "attempts": 1,
        "maxAttempts": 3,
        "createdAt": "2026-01-01T00:00:00Z",
        "queuedAt": "2026-01-01T00:00:01Z",
        "startedAt": "2026-01-01T00:00:02Z",
        "endedAt": None,
        "error": None,
        "payload": {"workflow": "standard"},
    }

    asyncio.run(repository.write(job))
    recovered = asyncio.run(repository.recover_interrupted_jobs())

    assert len(recovered) == 1
    assert recovered[0]["id"] == "job-1"
    assert recovered[0]["status"] == "queued"
    assert recovered[0]["startedAt"] is None
    assert recovered[0]["requeuedAt"] is not None


def test_job_repository_claims_queued_job_once(tmp_path) -> None:
    repository = JobRepository(Database(tmp_path / "osce_marker.sqlite3"))
    job = {
        "id": "job-1",
        "sessionId": "session-1",
        "taskType": "process_session",
        "status": "queued",
        "attempts": 0,
        "maxAttempts": 3,
        "createdAt": "2026-01-01T00:00:00Z",
        "queuedAt": "2026-01-01T00:00:01Z",
        "startedAt": None,
        "endedAt": None,
        "error": None,
        "payload": {"workflow": "standard"},
    }

    asyncio.run(repository.write(job))
    first_claim = asyncio.run(repository.claim_queued("job-1", "worker-1"))
    second_claim = asyncio.run(repository.claim_queued("job-1", "worker-2"))

    assert first_claim is not None
    assert first_claim["status"] == "running"
    assert first_claim["attempts"] == 1
    assert second_claim is None


def test_job_repository_prepares_failed_job_for_hatchet_retry(tmp_path) -> None:
    repository = JobRepository(Database(tmp_path / "osce_marker.sqlite3"))
    job = {
        "id": "job-1",
        "sessionId": "session-1",
        "taskType": "process_session",
        "status": "failed",
        "attempts": 1,
        "maxAttempts": 3,
        "createdAt": "2026-01-01T00:00:00Z",
        "queuedAt": "2026-01-01T00:00:01Z",
        "startedAt": "2026-01-01T00:00:02Z",
        "endedAt": "2026-01-01T00:00:03Z",
        "error": "transient",
        "payload": {"workflow": "standard"},
    }

    asyncio.run(repository.write(job))
    prepared = asyncio.run(repository.prepare_retry_attempt("job-1", "retry"))

    assert prepared["status"] == "queued"
    assert prepared["attempts"] == 1
    assert prepared["startedAt"] is None
    assert prepared["endedAt"] is None
    assert prepared["error"] == "retry"


def test_job_repository_does_not_requeue_cancelled_interrupted_job(tmp_path) -> None:
    repository = JobRepository(Database(tmp_path / "osce_marker.sqlite3"))
    job = {
        "id": "job-1",
        "sessionId": "session-1",
        "taskType": "process_session",
        "status": "cancelled",
        "attempts": 1,
        "maxAttempts": 3,
        "createdAt": "2026-01-01T00:00:00Z",
        "queuedAt": "2026-01-01T00:00:01Z",
        "startedAt": "2026-01-01T00:00:02Z",
        "endedAt": "2026-01-01T00:00:03Z",
        "error": "cancelled",
        "payload": {"workflow": "standard"},
    }

    asyncio.run(repository.write(job))
    requeued = asyncio.run(repository.requeue_interrupted_job("job-1", "interrupted"))

    assert requeued["status"] == "cancelled"
    assert requeued["error"] == "cancelled"
