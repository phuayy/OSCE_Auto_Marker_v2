"""The cached view of the accounts table that every request verifies against."""

from __future__ import annotations

import asyncio

from app.database.change_tracking import install_change_tracking
from app.database.orm import OrmDatabase
from app.domain.users import UserRole, UserStatus
from app.repositories.user_repository import UserRepository
from app.services.change_feed_service import ChangeFeedService
from app.services.user_directory import UserDirectory


async def _build(tmp_path):
    database = OrmDatabase(tmp_path / "app.sqlite3")
    await database.initialize()
    await install_change_tracking(database.engine)
    changes = ChangeFeedService(database)
    await changes.start(push_enabled=False)
    repository = UserRepository(database)
    directory = UserDirectory(repository, changes=changes)
    return database, changes, repository, directory


def test_a_hit_costs_no_repository_read_and_a_write_evicts(tmp_path) -> None:
    async def scenario() -> None:
        database, changes, repository, directory = await _build(tmp_path)
        try:
            record = await repository.create(
                username="admin",
                email=None,
                display_name="",
                role=UserRole.ADMIN,
                status=UserStatus.ACTIVE,
                password_hash="x",
            )
            first = await directory.get(record.id)
            assert first is not None and first.active and first.token_version == 1

            calls = {"reads": 0}
            original = repository.list_all

            async def counting():
                calls["reads"] += 1
                return await original()

            repository.list_all = counting  # type: ignore[method-assign]
            assert await directory.get(record.id) is first
            assert calls["reads"] == 0

            # A write anywhere bumps the counter; the next read rebuilds.
            await repository.update(record.id, lambda row: setattr(row, "token_version", 2))
            refreshed = await directory.get(record.id)
            assert refreshed is not None and refreshed.token_version == 2
            assert calls["reads"] == 1
        finally:
            await changes.stop()
            await database.shutdown()

    asyncio.run(scenario())


def test_rows_with_an_unknown_role_or_status_are_treated_as_absent(tmp_path) -> None:
    async def scenario() -> None:
        database, changes, repository, directory = await _build(tmp_path)
        try:
            record = await repository.create(
                username="odd",
                email=None,
                display_name="",
                role=UserRole.MARKER,
                status=UserStatus.ACTIVE,
                password_hash="x",
            )
            await repository.update(record.id, lambda row: setattr(row, "role", "superuser"))
            assert await directory.get(record.id) is None
        finally:
            await changes.stop()
            await database.shutdown()

    asyncio.run(scenario())


def test_without_a_change_feed_the_directory_reads_through(tmp_path) -> None:
    async def scenario() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        await database.initialize()
        try:
            repository = UserRepository(database)
            directory = UserDirectory(repository)
            assert directory.cache_stats()["enabled"] is False
            record = await repository.create(
                username="a",
                email=None,
                display_name="",
                role=UserRole.ADMIN,
                status=UserStatus.ACTIVE,
                password_hash="x",
            )
            assert (await directory.get(record.id)) is not None
            await repository.update(record.id, lambda row: setattr(row, "status", UserStatus.DISABLED.value))
            snapshot = await directory.get(record.id)
            assert snapshot is not None and not snapshot.active
        finally:
            await database.shutdown()

    asyncio.run(scenario())
