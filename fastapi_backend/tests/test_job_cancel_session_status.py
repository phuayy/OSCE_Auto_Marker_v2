"""Cancelling a job that owns its session's status must end the session.

Bug: ``JobQueueService.cancel`` marked the job row cancelled and mirrored the
row onto ``session.job`` but never moved ``session.status``, so a cancelled
``queued``/``processing`` session stayed in flight (un-openable, ``/rerun``
refused it) until a restart's reconciliation — which never runs on Hatchet.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.config import Settings
from app.database.orm import OrmDatabase
from app.repositories.job_repository import JobRepository
from app.services.event_service import EventService
from app.services.job_queue_service import JobQueueService
from tests.fixtures.session_store import SessionUpdateMixin


class FakeSessions(SessionUpdateMixin):
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}

    async def read(self, session_id: str) -> dict[str, Any]:
        if session_id not in self.sessions:
            raise FileNotFoundError(session_id)
        return {**self.sessions[session_id]}

    async def write(self, session: dict[str, Any]) -> None:
        self.sessions[str(session["id"])] = dict(session)


class FakeStorage:
    async def prepare_session_sources(self, session: dict[str, Any]) -> dict[str, Any]:
        return session


def _service(tmp_path, backend: str = "local") -> tuple[JobQueueService, FakeSessions]:
    settings = Settings(
        ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", scorer_python_bin="python", job_queue_backend=backend
    )
    sessions = FakeSessions()
    service = JobQueueService(
        settings, JobRepository(OrmDatabase(tmp_path / "osce.sqlite3")), EventService(settings), sessions, FakeStorage()
    )
    service.bind_handlers(pipeline=object(), clips=object())
    return service, sessions


async def _queued(service: JobQueueService, sessions: FakeSessions, task_type: str, status: str = "queued") -> dict:
    await service.repository.initialize()
    sessions.sessions["s1"] = {"id": "s1", "status": status}
    job = await service.enqueue("s1", task_type, {}, auto_start=False)
    await service._sync_session_job(job)
    return job


@pytest.mark.parametrize("backend", ["local", "hatchet"])
def test_cancelling_a_queued_job_ends_the_session(tmp_path, backend) -> None:
    service, sessions = _service(tmp_path, backend)

    async def scenario() -> None:
        job = await _queued(service, sessions, "process_session")
        assert sessions.sessions["s1"]["status"] == "queued"
        await service.cancel(str(job["id"]), "Job cancelled by API request.")

    asyncio.run(scenario())
    session = sessions.sessions["s1"]
    assert session["status"] == "cancelled"
    assert session["error"] == "Job cancelled by API request."
    assert session["job"]["status"] == "cancelled"


def test_cancelling_a_running_job_ends_the_session(tmp_path) -> None:
    service, sessions = _service(tmp_path)

    async def scenario() -> None:
        job = await _queued(service, sessions, "auto_crop")
        sessions.sessions["s1"]["status"] = "processing"
        await service.cancel(str(job["id"]))

    asyncio.run(scenario())
    assert sessions.sessions["s1"]["status"] == "cancelled"


def test_a_cancelled_session_can_be_rerun(tmp_path) -> None:
    from app.domain.sessions import IN_FLIGHT_STATUSES

    service, sessions = _service(tmp_path)
    asyncio.run(self_cancel(service, sessions))
    assert sessions.sessions["s1"]["status"] not in IN_FLIGHT_STATUSES


async def self_cancel(service: JobQueueService, sessions: FakeSessions) -> None:
    job = await _queued(service, sessions, "process_session")
    await service.cancel(str(job["id"]))


def test_cancelling_an_old_job_leaves_a_newer_run_alone(tmp_path) -> None:
    service, sessions = _service(tmp_path)

    async def scenario() -> None:
        old = await _queued(service, sessions, "process_session")
        # The user re-ran: a newer job now owns the session.
        await service.repository.mark_cancelled(str(old["id"]), "superseded")
        newer = await service.enqueue("s1", "process_session", {}, auto_start=False)
        await service._sync_session_job(newer)
        sessions.sessions["s1"]["status"] = "processing"
        await service._sync_session_job({**old, "status": "cancelled"})

    asyncio.run(scenario())
    session = sessions.sessions["s1"]
    assert session["status"] == "processing"
    assert session["job"]["status"] != "cancelled"


def test_cancelling_a_finished_job_changes_nothing(tmp_path) -> None:
    service, sessions = _service(tmp_path)

    async def scenario() -> None:
        job = await _queued(service, sessions, "process_session")
        await service.repository.mark_cancelled(str(job["id"]), "x")  # already terminal
        sessions.sessions["s1"]["status"] = "completed"
        await service.cancel(str(job["id"]))

    asyncio.run(scenario())
    assert sessions.sessions["s1"]["status"] == "completed"


@pytest.mark.parametrize("task_type", ["export_clips", "prepare_media"])
def test_side_tasks_leave_the_session_status_alone(tmp_path, task_type) -> None:
    service, sessions = _service(tmp_path)

    async def scenario() -> None:
        job = await _queued(service, sessions, task_type, status="cropped")
        await service.cancel(str(job["id"]))

    asyncio.run(scenario())
    assert sessions.sessions["s1"]["status"] == "cropped"
    assert sessions.sessions["s1"].get("error") is None


def test_purging_a_session_survives_the_session_being_gone(tmp_path) -> None:
    service, sessions = _service(tmp_path)

    async def scenario() -> None:
        await _queued(service, sessions, "process_session")
        del sessions.sessions["s1"]
        await service.purge_session("s1")

    asyncio.run(scenario())
