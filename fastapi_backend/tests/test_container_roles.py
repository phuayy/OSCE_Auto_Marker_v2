"""Role-aware startup (F8).

One ``startup()`` served two processes. In the Hatchet worker it ran the API's
schema migration, seed data and — the harmful part — the upload recovery
sweeps, which read "an upload still assembling at boot was killed by the
restart" and failed uploads the API was assembling at that moment. It also
rebuilt the whole container for every job. These tests pin what each role
does and does not do, and that a worker process binds one container.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.queue import hatchet_tasks
from app.services import container as container_module
from app.services.container import ContainerRole, create_container


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    overrides.setdefault("job_queue_backend", "hatchet")
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
        transcription_prefetch_models=True,
        **overrides,
    )


class _Calls:
    """Replaces the startup steps whose *invocation* is the thing under test."""

    def __init__(self, container, monkeypatch: pytest.MonkeyPatch) -> None:
        self.names: list[str] = []

        def record(name: str):
            async def _stub(*_args: Any, **_kwargs: Any) -> Any:
                self.names.append(name)
                return None

            return _stub

        monkeypatch.setattr(container_module, "run_database_migrations", record("migrate"))
        monkeypatch.setattr(container.async_uploads, "recover_stale_assembling_uploads", record("assembly_sweep"))
        monkeypatch.setattr(container.async_uploads, "recover_expired_uploads", record("expiry_sweep"))
        monkeypatch.setattr(container.sessions, "migrate_legacy_sessions", record("legacy_sessions"))
        monkeypatch.setattr(container.rubrics, "ensure_parsed", record("rubric_parse"))
        monkeypatch.setattr(container.transcription, "prefetch_selected_engine", record("prefetch"))

        original_jobs_startup = container.jobs.startup

        async def jobs_startup(**kwargs: Any) -> None:
            self.names.append(f"jobs:{kwargs['dispatch_queued']}:{kwargs['recover_interrupted']}")
            await original_jobs_startup(**kwargs)

        monkeypatch.setattr(container.jobs, "startup", jobs_startup)


async def _boot(container, role: ContainerRole) -> None:
    await container.artifacts.ensure_storage_layout()
    await container.orm_database.initialize()
    try:
        await container.startup(role=role)
        # Let the background registry run the (stubbed) prefetch, if spawned.
        await asyncio.sleep(0)
    finally:
        await container.shutdown()


def test_worker_role_skips_the_api_sweeps_migrations_and_seed_data(tmp_path: Path, monkeypatch) -> None:
    container = create_container(_settings(tmp_path))
    calls = _Calls(container, monkeypatch)

    asyncio.run(_boot(container, ContainerRole.WORKER))

    assert "assembly_sweep" not in calls.names, "a worker failed uploads the API may be assembling"
    assert "expiry_sweep" not in calls.names
    assert "migrate" not in calls.names, "a worker raced the API on the schema"
    assert "legacy_sessions" not in calls.names and "rubric_parse" not in calls.names
    # Hatchet drives the worker's jobs: nothing recovered, nothing dispatched.
    assert "jobs:False:False" in calls.names
    # The worker transcribes, so it is the process that wants the weights.
    assert "prefetch" in calls.names


def test_api_role_runs_the_sweeps_and_recovery(tmp_path: Path, monkeypatch) -> None:
    container = create_container(_settings(tmp_path, job_queue_backend="local"))
    calls = _Calls(container, monkeypatch)

    asyncio.run(_boot(container, ContainerRole.API))

    for step in ("migrate", "legacy_sessions", "rubric_parse", "assembly_sweep", "expiry_sweep", "prefetch"):
        assert step in calls.names, f"the API role skipped {step}"
    assert "jobs:True:True" in calls.names


def test_api_role_with_a_remote_queue_does_not_prefetch_weights_it_will_never_use(tmp_path: Path, monkeypatch) -> None:
    container = create_container(_settings(tmp_path, job_queue_backend="hatchet"))
    calls = _Calls(container, monkeypatch)

    asyncio.run(_boot(container, ContainerRole.API))

    assert "assembly_sweep" in calls.names
    assert "prefetch" not in calls.names, "an API that only enqueues downloaded a multi-GB checkpoint"
    assert "jobs:True:False" in calls.names


@pytest.mark.parametrize("role", [ContainerRole.API, ContainerRole.WORKER])
def test_startup_fails_before_starting_services_when_tracking_is_missing(tmp_path, monkeypatch, role) -> None:
    from unittest.mock import AsyncMock

    container = create_container(_settings(tmp_path, db_auto_migrate=False))
    start = AsyncMock()
    monkeypatch.setattr(container.changes, "start", start)

    async def scenario() -> None:
        await container.artifacts.ensure_storage_layout()
        await container.orm_database.initialize(migrate=True)
        async with container.orm_database.engine.begin() as connection:
            await connection.exec_driver_sql("DROP TRIGGER trg_users_change_update")
        try:
            with pytest.raises(RuntimeError, match="trg_users_change_update"):
                await container.startup(role=role)
            start.assert_not_awaited()
        finally:
            await container.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("role,auto_migrate", [
    (ContainerRole.API, False), (ContainerRole.WORKER, False), (ContainerRole.WORKER, True),
])
def test_startup_does_not_bootstrap_when_migration_is_disabled(tmp_path, role, auto_migrate) -> None:
    from sqlalchemy import inspect

    container = create_container(_settings(tmp_path, db_auto_migrate=auto_migrate))

    async def scenario() -> None:
        try:
            with pytest.raises(RuntimeError, match="alembic upgrade head"):
                await container.startup(role=role)
            async with container.orm_database.engine.connect() as connection:
                assert await connection.run_sync(lambda sync: inspect(sync).get_table_names()) == []
        finally:
            await container.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("role", [ContainerRole.API, ContainerRole.WORKER])
def test_lifespan_disposes_resources_when_startup_fails(monkeypatch, role) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    container = SimpleNamespace(
        startup=AsyncMock(side_effect=RuntimeError("schema unavailable")),
        shutdown=AsyncMock(),
    )

    async def scenario() -> None:
        if role is ContainerRole.API:
            from app import main

            monkeypatch.setattr(main, "create_container", lambda: container)
            with pytest.raises(RuntimeError, match="schema unavailable"):
                async with main.lifespan(SimpleNamespace(state=SimpleNamespace())):
                    pytest.fail("startup failure must prevent serving")
        else:
            from app.queue import hatchet_worker

            monkeypatch.setattr(hatchet_worker, "create_container", lambda _settings: container)
            with pytest.raises(RuntimeError, match="schema unavailable"):
                await anext(hatchet_worker._worker_lifespan())
        container.shutdown.assert_awaited_once()

    asyncio.run(scenario())


def test_hatchet_task_reuses_the_container_the_worker_bound(tmp_path: Path) -> None:
    sentinel = object()
    hatchet_tasks.bind_worker_container(sentinel)  # type: ignore[arg-type]
    try:
        assert asyncio.run(hatchet_tasks.get_worker_container()) is sentinel
    finally:
        hatchet_tasks.bind_worker_container(None)
