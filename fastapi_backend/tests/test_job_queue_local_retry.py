"""Local-backend job retries.

``maxAttempts`` was honoured only by the Hatchet path: in ``local`` mode a
failed job was marked failed and abandoned, so a transient fault (an upstream
5xx, a GPU OOM, a flaky subprocess) was terminal on the first try. These tests
pin the retry loop that the local runner now owns, and the boundaries that keep
it from becoming a retry storm: a permanent 4xx is never retried, attempts stop
at ``maxAttempts``, and Hatchet's own policy is left untouched.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.config import Settings
from app.core.exceptions import AppError, EmptyTranscriptError
from sqlalchemy import select

from app.database.models import JobEventRecord
from app.database.orm import OrmDatabase
from app.repositories.job_repository import JobRepository
from app.services.event_service import EventService
from app.services.job_queue_service import JobQueueService, JobRunResult, is_retryable_failure
from tests.fixtures.session_store import SessionUpdateMixin


class FakeSessions(SessionUpdateMixin):
    """Minimal session store: the queue only reads, mutates and writes back."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}

    async def read(self, session_id: str) -> dict[str, Any]:
        if session_id not in self.sessions:
            self.sessions[session_id] = {"id": session_id, "status": "queued"}
        return dict(self.sessions[session_id])

    async def write(self, session: dict[str, Any]) -> None:
        self.sessions[str(session["id"])] = dict(session)


class FakeStorage:
    async def prepare_session_sources(self, session: dict[str, Any]) -> dict[str, Any]:
        return session


class ScriptedPipeline:
    """Fails for the first ``failures`` attempts, then succeeds.

    ``error_factory`` decides *how* it fails, which is what separates a
    retryable fault from a permanent rejection.
    """

    def __init__(self, failures: int, error_factory=lambda attempt: RuntimeError(f"boom {attempt}")) -> None:
        self.failures = failures
        self.error_factory = error_factory
        self.attempts = 0
        self.failed_sessions: list[str] = []

    async def process_session_by_id(self, _session_id: str, *, allow_processing: bool = False) -> dict[str, Any]:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise self.error_factory(self.attempts)
        return {"ok": True}

    async def mark_session_failed(self, session_id: str, _error: Exception) -> None:
        self.failed_sessions.append(session_id)


def make_service(
    tmp_path,
    pipeline: ScriptedPipeline,
    **setting_overrides: Any,
) -> JobQueueService:
    # Settings field defaults are read from the ambient environment (and the
    # repo's .env) at import time, so the backend under test is pinned here
    # rather than inherited from whatever the developer's .env happens to say.
    setting_overrides.setdefault("job_queue_backend", "local")
    setting_overrides.setdefault("local_job_auto_start", True)
    settings = Settings(
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        **setting_overrides,
    )
    repository = JobRepository(OrmDatabase(tmp_path / "osce_marker.sqlite3"))
    service = JobQueueService(settings, repository, EventService(settings), FakeSessions(), FakeStorage())
    service.bind_handlers(pipeline=pipeline, clips=object())
    return service


async def run_until_settled(service: JobQueueService, session_id: str) -> dict[str, Any]:
    """Enqueue a job and drive the local runner to completion."""
    await service.repository.initialize()
    job = await service.enqueue(session_id, "process_session", {}, auto_start=True)
    task = service._tasks.get(str(job["id"]))
    if task is not None:
        await task
    return await service.repository.read(str(job["id"]))


def statuses(events: list[tuple[str, str, dict[str, Any]]]) -> list[str]:
    return [str(payload.get("code")) for _session, name, payload in events if name == "status"]


async def job_event_types(service: JobQueueService, job_id: str) -> list[str]:
    """Read the job's audit trail straight from the table.

    ``append_event`` is write-only in production code, so the test reads the
    rows rather than growing the repository an API nothing else needs.
    """

    async with service.repository.database.session() as db:
        rows = await db.scalars(
            select(JobEventRecord.event_type)
            .where(JobEventRecord.job_id == job_id)
            .order_by(JobEventRecord.id.asc())
        )
        return [str(event_type) for event_type in rows]


# --- retryability rule ------------------------------------------------------


def test_client_errors_are_permanent_and_server_errors_are_transient() -> None:
    assert is_retryable_failure(RuntimeError("subprocess died")) is True
    assert is_retryable_failure(AppError("upstream unavailable", status_code=503)) is True
    assert is_retryable_failure(AppError("unsupported task", status_code=500)) is True
    assert is_retryable_failure(AppError("missing video", status_code=400)) is False
    assert is_retryable_failure(EmptyTranscriptError("no speech")) is False


# --- the retry loop ---------------------------------------------------------


def test_transient_failure_is_retried_until_it_succeeds(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.services.job_queue_service.compute_retry_backoff_seconds", lambda *_a, **_k: 0.0)
    pipeline = ScriptedPipeline(failures=2)
    service = make_service(tmp_path, pipeline)

    job = asyncio.run(run_until_settled(service, "session-retry"))

    assert pipeline.attempts == 3
    assert job["status"] == "succeeded"
    assert job["attempts"] == 3
    # The user is never shown a dead session for a fault that recovered.
    assert pipeline.failed_sessions == []


def test_retries_stop_at_max_attempts_and_the_session_is_failed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.services.job_queue_service.compute_retry_backoff_seconds", lambda *_a, **_k: 0.0)
    pipeline = ScriptedPipeline(failures=99)
    service = make_service(tmp_path, pipeline)

    job = asyncio.run(run_until_settled(service, "session-exhausted"))

    assert pipeline.attempts == 3  # local default maxAttempts
    assert job["status"] == "failed"
    assert job["attempts"] == 3
    assert pipeline.failed_sessions == ["session-exhausted"]


def test_permanent_failure_is_not_retried(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.services.job_queue_service.compute_retry_backoff_seconds", lambda *_a, **_k: 0.0)
    pipeline = ScriptedPipeline(
        failures=99,
        error_factory=lambda _attempt: EmptyTranscriptError("no usable speech segments"),
    )
    service = make_service(tmp_path, pipeline)

    job = asyncio.run(run_until_settled(service, "session-permanent"))

    assert pipeline.attempts == 1
    assert job["status"] == "failed"
    assert pipeline.failed_sessions == ["session-permanent"]


def test_retry_is_recorded_on_the_job_event_log(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.services.job_queue_service.compute_retry_backoff_seconds", lambda *_a, **_k: 0.0)
    pipeline = ScriptedPipeline(failures=1)
    service = make_service(tmp_path, pipeline)

    async def scenario() -> list[str]:
        job = await run_until_settled(service, "session-audit")
        return await job_event_types(service, str(job["id"]))

    event_types = asyncio.run(scenario())

    assert "failed" in event_types
    assert "retry_scheduled" in event_types
    assert "succeeded" in event_types


def test_exhausted_retries_are_recorded_as_skipped(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.services.job_queue_service.compute_retry_backoff_seconds", lambda *_a, **_k: 0.0)
    pipeline = ScriptedPipeline(failures=99)
    service = make_service(tmp_path, pipeline)

    async def scenario() -> list[str]:
        job = await run_until_settled(service, "session-audit-2")
        return await job_event_types(service, str(job["id"]))

    event_types = asyncio.run(scenario())

    assert "retry_skipped" in event_types


def test_session_stays_non_terminal_while_a_retry_is_pending(tmp_path, monkeypatch) -> None:
    """A retry that is still coming must not surface as a failed session, or the
    UI shows a dead card for work that is about to run again."""
    monkeypatch.setattr("app.services.job_queue_service.compute_retry_backoff_seconds", lambda *_a, **_k: 0.0)
    pipeline = ScriptedPipeline(failures=1)
    service = make_service(tmp_path, pipeline)
    captured: list[tuple[str, str, dict[str, Any]]] = []

    async def capture(session_id: str, event_name: str, payload: dict[str, Any] | None = None) -> None:
        captured.append((session_id, event_name, dict(payload or {})))

    service.events.publish = capture  # type: ignore[method-assign]

    asyncio.run(run_until_settled(service, "session-status"))

    assert "failed" not in statuses(captured)
    assert statuses(captured).count("queued") >= 2  # initial enqueue + the retry
    assert statuses(captured)[-1] == "succeeded"


# --- backend boundaries -----------------------------------------------------


def test_hatchet_backend_does_not_retry_locally(tmp_path) -> None:
    """Hatchet owns its own retry policy; a second one here would double-run."""
    service = make_service(tmp_path, ScriptedPipeline(failures=1), job_queue_backend="hatchet")
    failed_job = {"id": "job-1", "sessionId": "s1", "attempts": 1, "maxAttempts": 3}

    assert service._local_retry_available(failed_job, RuntimeError("boom")) is False


def test_manual_start_mode_does_not_retry(tmp_path) -> None:
    """With auto-start off, nothing runs jobs in-process — so nothing can retry
    one either, and claiming otherwise would strand the job as 'queued'."""
    service = make_service(tmp_path, ScriptedPipeline(failures=1), local_job_auto_start=False)
    failed_job = {"id": "job-1", "sessionId": "s1", "attempts": 1, "maxAttempts": 3}

    assert service._local_retry_available(failed_job, RuntimeError("boom")) is False


def test_local_backend_retry_predicate_respects_remaining_attempts(tmp_path) -> None:
    service = make_service(tmp_path, ScriptedPipeline(failures=1))

    assert service._local_retry_available({"attempts": 1, "maxAttempts": 3}, RuntimeError("boom")) is True
    assert service._local_retry_available({"attempts": 3, "maxAttempts": 3}, RuntimeError("boom")) is False


def test_arm_local_retry_ignores_a_result_that_did_not_fail(tmp_path) -> None:
    service = make_service(tmp_path, ScriptedPipeline(failures=0))

    assert asyncio.run(service._arm_local_retry(JobRunResult())) is False
    assert asyncio.run(service._arm_local_retry(JobRunResult(job={"id": "job-1"}))) is False


def test_run_job_reports_the_failure_to_its_caller(tmp_path) -> None:
    """The scheduler decides on a retry, so the executor has to hand back what
    happened rather than swallowing it."""
    pipeline = ScriptedPipeline(failures=1)
    service = make_service(tmp_path, pipeline)

    async def scenario() -> JobRunResult:
        await service.repository.initialize()
        job = await service.enqueue("session-result", "process_session", {}, auto_start=False)
        return await service.run_job(str(job["id"]))

    result = asyncio.run(scenario())

    assert result.failed is True
    assert isinstance(result.error, RuntimeError)
    assert result.job is not None and result.job["status"] == "failed"


def test_hatchet_path_still_raises_and_fails_the_session(tmp_path) -> None:
    """``raise_on_error`` is how the Hatchet worker learns a run failed; the
    retry work must not have swallowed it."""
    pipeline = ScriptedPipeline(failures=1)
    service = make_service(tmp_path, pipeline, job_queue_backend="hatchet")

    async def scenario() -> None:
        await service.repository.initialize()
        job = await service.enqueue("session-hatchet", "process_session", {}, auto_start=False)
        await service.run_job(str(job["id"]), raise_on_error=True)

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(scenario())
    assert pipeline.failed_sessions == ["session-hatchet"]
