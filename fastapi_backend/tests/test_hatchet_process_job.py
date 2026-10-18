"""``_run_process_job`` — the Hatchet task body, decoupled from the decorator.

A retry that lands after ``purge_session`` has deleted the job row used to
raise an unhandled ``FileNotFoundError`` out of the Hatchet task (via
``prepare_hatchet_retry_attempt`` -> ``JobRepository.prepare_retry_attempt``).
These tests pin the graceful path: a missing job is reported and skipped, never
raised, and an existing job still runs exactly as before.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.queue.hatchet_tasks import _run_process_job


class FakeJobQueueService:
    def __init__(self, *, exists: bool, run_result: Any = "completed") -> None:
        self.exists = exists
        self.run_result = run_result
        self.prepare_calls: list[tuple[str, int]] = []
        self.run_calls: list[tuple[str, bool]] = []

    async def prepare_hatchet_retry_attempt(self, job_id: str, retry_count: int) -> bool:
        self.prepare_calls.append((job_id, retry_count))
        return self.exists

    async def run_job(self, job_id: str, *, raise_on_error: bool = False) -> Any:
        self.run_calls.append((job_id, raise_on_error))
        if isinstance(self.run_result, Exception):
            raise self.run_result
        return self.run_result


class FakeContainer:
    def __init__(self, jobs: FakeJobQueueService) -> None:
        self.jobs = jobs


def test_process_job_skips_and_never_runs_a_purged_job() -> None:
    jobs = FakeJobQueueService(exists=False)
    container = FakeContainer(jobs)

    result = asyncio.run(_run_process_job("job-1", 2, container))

    assert result == {"jobId": "job-1", "status": "skipped_missing"}
    assert jobs.prepare_calls == [("job-1", 2)]
    assert jobs.run_calls == []  # never touches run_job for a job that is gone


def test_process_job_runs_normally_when_the_job_still_exists() -> None:
    jobs = FakeJobQueueService(exists=True)
    container = FakeContainer(jobs)

    result = asyncio.run(_run_process_job("job-2", 0, container))

    assert result == {"jobId": "job-2", "status": "completed"}
    assert jobs.run_calls == [("job-2", True)]


def test_process_job_propagates_a_genuine_execution_failure() -> None:
    """Only the missing-job race is swallowed; a real failure must still reach
    Hatchet so its retry policy (and session-failure bookkeeping) applies."""
    jobs = FakeJobQueueService(exists=True, run_result=RuntimeError("scorer crashed"))
    container = FakeContainer(jobs)

    try:
        asyncio.run(_run_process_job("job-3", 1, container))
        raised = False
    except RuntimeError as error:
        raised = True
        assert "scorer crashed" in str(error)

    assert raised


def test_process_job_tells_hatchet_not_to_retry_a_final_failure() -> None:
    """A job already failed for good must not be re-run by Hatchet's policy."""
    from app.services.job_queue_service import FinalJobFailure

    jobs = FakeJobQueueService(exists=True, run_result=FinalJobFailure("no speech"))
    container = FakeContainer(jobs)

    try:
        asyncio.run(_run_process_job("job-4", 0, container))
        raised: Exception | None = None
    except Exception as error:  # noqa: BLE001
        raised = error

    assert raised is not None and "no speech" in str(raised)
    assert isinstance(raised.__cause__, FinalJobFailure)
    try:
        from hatchet_sdk import NonRetryableException
    except ImportError:
        return
    assert isinstance(raised, NonRetryableException)
