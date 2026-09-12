"""Starting work on a stored session queues a job; nothing runs in the request (F6).

``POST /process``, ``POST /auto-crop`` and clip assessment used to await the
pipeline inside the handler. A proxy timeout or a restart ended the request
with the session stuck in ``processing`` and no job row for recovery to find.
These tests pin the replacement: the session flips to ``queued``, a job row
exists, the response carries both, and the guards that keep a session from
being queued twice or without its video.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.core.exceptions import AppError
from app.services.container import create_container


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        app_database_url="",
        database_url="",
        job_queue_backend="local",
        # Queue only: the point of these tests is what the request does, not
        # whether the (stubbed) pipeline would then succeed.
        local_job_auto_start=False,
    )


async def _container(tmp_path: Path, session: dict[str, Any]):
    container = create_container(_settings(tmp_path))
    await container.artifacts.ensure_storage_layout()
    await container.storage.ensure_layout()
    await container.database.initialize()
    await container.orm_database.initialize()
    await container.jobs.repository.initialize()
    await container.sessions.write(session)
    return container


def _session(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    video = tmp_path / "station.mp4"
    video.write_bytes(b"video")
    session = {
        "id": "s1",
        "name": "Station",
        "status": "uploaded",
        "workflow": "standard",
        "files": {"video": {"fileName": "station.mp4", "absolutePath": str(video)}},
        "outputs": {},
    }
    session.update(overrides)
    return session


def test_start_processing_queues_a_job_and_flips_the_session_to_queued(tmp_path: Path) -> None:
    async def scenario() -> None:
        container = await _container(tmp_path, _session(tmp_path))

        result = await container.session_maintenance.start_processing("s1")

        assert result["session"]["status"] == "queued"
        assert result["job"]["taskType"] == "process_session"
        assert result["job"]["status"] == "queued"
        stored = await container.sessions.read("s1")
        assert stored["status"] == "queued"
        assert stored["job"]["id"] == result["job"]["id"]
        jobs = await container.jobs.list_jobs("s1")
        assert [job["taskType"] for job in jobs] == ["process_session"]

    asyncio.run(scenario())


def test_start_auto_crop_queues_the_segmentation_job(tmp_path: Path) -> None:
    async def scenario() -> None:
        container = await _container(
            tmp_path, _session(tmp_path, workflow="long", segmentation="person")
        )

        result = await container.session_maintenance.start_auto_crop("s1")

        assert result["job"]["taskType"] == "auto_crop"
        assert (await container.sessions.read("s1"))["status"] == "queued"

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["queued", "processing", "assembling"])
def test_an_in_flight_session_is_not_queued_twice(tmp_path: Path, status: str) -> None:
    async def scenario() -> None:
        container = await _container(tmp_path, _session(tmp_path, status=status))
        with pytest.raises(AppError) as excinfo:
            await container.session_maintenance.start_processing("s1")
        assert excinfo.value.status_code == 409
        assert await container.jobs.list_jobs("s1") == []

    asyncio.run(scenario())


def test_a_session_without_its_video_is_refused_before_anything_is_queued(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = _session(tmp_path)
        session["files"]["video"]["absolutePath"] = str(tmp_path / "gone.mp4")
        container = await _container(tmp_path, session)
        with pytest.raises(AppError) as excinfo:
            await container.session_maintenance.start_processing("s1")
        assert excinfo.value.status_code == 400
        assert (await container.sessions.read("s1"))["status"] == "uploaded"
        assert await container.jobs.list_jobs("s1") == []

    asyncio.run(scenario())
