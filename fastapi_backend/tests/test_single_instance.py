"""Only one API process may hold a storage root's in-process state at a
time — token revocation, the login/token rate limiters and the per-upload
part lock. See app/core/single_instance.py and CLAUDE.md "Host hardening".
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.single_instance import SingleInstanceError, SingleInstanceLock
from app.services.container import ContainerRole, create_container


def _settings(tmp_path: Path, **overrides) -> Settings:
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


# --- the lock primitive -------------------------------------------------------


def test_a_second_lock_on_the_same_path_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "run" / "api.lock"
    first = SingleInstanceLock(path)
    first.acquire()
    try:
        second = SingleInstanceLock(path)
        with pytest.raises(SingleInstanceError):
            second.acquire()
    finally:
        first.release()


def test_releasing_lets_another_process_acquire_it(tmp_path: Path) -> None:
    path = tmp_path / "run" / "api.lock"
    first = SingleInstanceLock(path)
    first.acquire()
    first.release()

    second = SingleInstanceLock(path)
    second.acquire()  # must not raise
    second.release()


def test_acquiring_twice_on_the_same_instance_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "run" / "api.lock"
    lock = SingleInstanceLock(path)
    lock.acquire()
    try:
        lock.acquire()  # must not raise or deadlock
    finally:
        lock.release()


def test_the_lock_file_records_the_holders_pid(tmp_path: Path) -> None:
    import os

    path = tmp_path / "run" / "api.lock"
    lock = SingleInstanceLock(path)
    lock.acquire()
    try:
        # Read through the lock's own handle: on Windows, msvcrt's mandatory
        # byte-range lock also blocks a *second* open of the same region, even
        # from this process, so a fresh Path.read_text() here would deadlock
        # against the very lock this test is confirming works.
        lock._handle.seek(0)
        assert lock._handle.read().strip() == str(os.getpid())
    finally:
        lock.release()


def test_releasing_an_unacquired_lock_does_nothing(tmp_path: Path) -> None:
    SingleInstanceLock(tmp_path / "run" / "api.lock").release()  # must not raise


# --- wired into container startup ---------------------------------------------


def test_a_second_api_boot_against_the_same_storage_root_is_refused(tmp_path: Path) -> None:
    first = create_container(_settings(tmp_path))
    second = create_container(_settings(tmp_path))

    async def scenario() -> None:
        await first.startup(role=ContainerRole.API)
        try:
            with pytest.raises(SingleInstanceError):
                await second.startup(role=ContainerRole.API)
        finally:
            await first.shutdown()

    asyncio.run(scenario())


def test_a_second_api_boot_is_allowed_after_the_first_shuts_down(tmp_path: Path) -> None:
    first = create_container(_settings(tmp_path))
    second = create_container(_settings(tmp_path))

    async def scenario() -> None:
        await first.startup(role=ContainerRole.API)
        await first.shutdown()
        await second.startup(role=ContainerRole.API)
        await second.shutdown()

    asyncio.run(scenario())


def test_a_worker_role_boot_takes_no_lock_and_never_conflicts_with_the_api(tmp_path: Path) -> None:
    api = create_container(_settings(tmp_path, job_queue_backend="hatchet"))
    worker = create_container(_settings(tmp_path, job_queue_backend="hatchet"))

    async def scenario() -> None:
        await api.startup(role=ContainerRole.API)
        try:
            await worker.startup(role=ContainerRole.WORKER)  # must not raise
            await worker.shutdown()
        finally:
            await api.shutdown()

    asyncio.run(scenario())


def test_allow_multiple_api_instances_opts_out_of_the_guard(tmp_path: Path) -> None:
    first = create_container(_settings(tmp_path, allow_multiple_api_instances=True))
    second = create_container(_settings(tmp_path, allow_multiple_api_instances=True))

    async def scenario() -> None:
        await first.startup(role=ContainerRole.API)
        try:
            await second.startup(role=ContainerRole.API)  # must not raise
            await second.shutdown()
        finally:
            await first.shutdown()

    asyncio.run(scenario())
