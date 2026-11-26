"""Session lifecycle: what retention may purge, what a re-run keeps, and the
periodic upload-expiry sweep.

* M5 - the video-retention sweep never takes the source from a session a run
  (or an upload) is still using, and the boot sweep runs after job recovery.
* M6 - a re-run keeps what is derived from the *source* video (the browser
  playback copy and poster); a delete still removes them.
* M28 - abandoned uploads are reclaimed on a timer, not only at startup.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.services import container as container_module
from app.services.container import ContainerRole, create_container


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    overrides.setdefault("session_video_retention_days", 365)
    return Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        auth_bcrypt_rounds=4,
        default_admin_password="admin",
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        app_database_url="",
        database_url="",
        **overrides,
    )


async def _init(container) -> None:
    await container.artifacts.ensure_storage_layout()
    await container.storage.ensure_layout()
    await container.orm_database.initialize()


async def _old_session(container, tmp_path: Path, session_id: str, status: str, **extra: Any) -> Path:
    video = tmp_path / f"{session_id}.mp4"
    video.write_text("x", encoding="utf-8")
    await container.sessions.write(
        {
            "id": session_id,
            "name": session_id,
            "status": status,
            "createdAt": "2020-01-01T00:00:00Z",
            "files": {"video": {"fileName": video.name, "absolutePath": str(video)}},
            "outputs": {},
            **extra,
        }
    )
    return video


# --- M5 ---------------------------------------------------------------------


@pytest.mark.parametrize("status", ["waiting_for_upload", "uploading", "assembling", "queued", "processing"])
def test_retention_never_purges_an_old_session_that_is_in_use(tmp_path, status) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        await _init(container)
        video = await _old_session(container, tmp_path, "busy", status)

        assert await container.session_maintenance.run_video_retention_sweep() == 0
        assert video.exists()
        # ...and the guard is not only the candidate query: asked directly, it still refuses.
        assert await container.session_maintenance.purge_expired_video("busy") is False
        assert video.exists()
        await container.shutdown()

    asyncio.run(_run())


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_retention_still_purges_old_finished_sessions(tmp_path, status) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        await _init(container)
        video = await _old_session(container, tmp_path, "done", status)

        assert await container.session_maintenance.run_video_retention_sweep() == 1
        assert not video.exists()
        await container.shutdown()

    asyncio.run(_run())


def test_a_session_that_goes_in_flight_between_listing_and_purge_is_skipped(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        await _init(container)
        video = await _old_session(container, tmp_path, "raced", "completed")
        repository = container.sessions.repository
        original = repository.list_video_retention_candidates

        async def list_then_requeue(*args: Any, **kwargs: Any) -> list[str]:
            candidates = await original(*args, **kwargs)
            await container.sessions.update("raced", lambda s: s.__setitem__("status", "queued"))
            return candidates

        monkeypatch.setattr(repository, "list_video_retention_candidates", list_then_requeue)

        assert await container.session_maintenance.run_video_retention_sweep() == 0
        assert video.exists()
        assert not ((await container.sessions.read("raced"))["files"]["video"].get("purgedAt"))
        await container.shutdown()

    asyncio.run(_run())


def test_purged_at_is_not_recorded_when_the_delete_fails(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        await _init(container)
        await _old_session(container, tmp_path, "stuck", "completed", files={
            "video": {"fileName": "v.mp4", "absolutePath": str(tmp_path / "stuck.mp4"),
                      "storageRef": {"provider": "gcs", "key": "k"}},
        })

        async def boom(_ref: Any) -> None:
            raise RuntimeError("bucket unreachable")

        monkeypatch.setattr(container.session_maintenance.storage, "delete_committed_object", boom)

        assert await container.session_maintenance.run_video_retention_sweep() == 0
        assert not (await container.sessions.read("stuck"))["files"]["video"].get("purgedAt")
        await container.shutdown()

    asyncio.run(_run())


def test_boot_retention_sweep_runs_after_job_recovery(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path, job_queue_backend="local"))
        order: list[str] = []

        async def stub(name: str) -> int:
            order.append(name)
            return 0

        monkeypatch.setattr(container_module, "run_database_migrations", lambda *_a, **_k: stub("migrate"))
        monkeypatch.setattr(container.session_maintenance, "run_video_retention_sweep", lambda: stub("retention"))
        original_startup = container.jobs.startup

        async def jobs_startup(**kwargs: Any) -> None:
            await original_startup(**kwargs)
            order.append("jobs")

        monkeypatch.setattr(container.jobs, "startup", jobs_startup)
        await container.artifacts.ensure_storage_layout()
        await container.orm_database.initialize()
        try:
            await container.startup(role=ContainerRole.API)
        finally:
            await container.shutdown()
        assert order.index("retention") > order.index("jobs")

    asyncio.run(_run())


