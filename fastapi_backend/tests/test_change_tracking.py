"""Alembic owns tracking; runtime validation must fail on incomplete tracking."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from app.database.change_tracking import TRACKED_TABLES, install_change_tracking
from app.database.orm import OrmDatabase


async def _counter(database: OrmDatabase, table: str) -> int:
    async with database.engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT version FROM table_versions WHERE table_name = :name"),
                {"name": table},
            )
        ).first()
    return int(row[0]) if row else 0


async def _insert_notification(database: OrmDatabase, notification_id: str) -> None:
    async with database.engine.begin() as connection:
        await connection.exec_driver_sql(
            "INSERT INTO notifications (id, session_id, title, body, created_at) "
            f"VALUES ('{notification_id}', 's-1', 'T', 'B', CURRENT_TIMESTAMP)"
        )


def test_validation_preserves_existing_tracking(tmp_path) -> None:
    """Read-only validation must leave every migrated trigger operational."""

    async def scenario() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        await database.initialize()

        await install_change_tracking(database.engine)

        before = await _counter(database, "notifications")
        await _insert_notification(database, "n-1")
        assert await _counter(database, "notifications") > before, (
            "validation must leave the notification trigger operational"
        )
        await database.shutdown()

    asyncio.run(scenario())


def test_every_tracked_table_present_in_the_schema_gets_a_trigger(tmp_path) -> None:
    async def scenario() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        await database.initialize()
        await install_change_tracking(database.engine)

        def _read(connection):
            return {row[0] for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()}

        async with database.engine.connect() as connection:
            triggers = await connection.run_sync(lambda sync: _read(sync))

        for table in TRACKED_TABLES:
            for operation in ("insert", "update", "delete"):
                assert f"trg_{table}_change_{operation}" in triggers
        await database.shutdown()

    asyncio.run(scenario())


def test_installation_is_idempotent(tmp_path) -> None:
    """Runs on every boot, and re-running must not double-count a single write."""

    async def scenario() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        await database.initialize()

        await install_change_tracking(database.engine)
        await install_change_tracking(database.engine)

        before = await _counter(database, "notifications")
        await _insert_notification(database, "n-2")
        after = await _counter(database, "notifications")

        assert after - before == 1, "re-installing must replace triggers, not stack them"
        await database.shutdown()

    asyncio.run(scenario())


def test_sqlite_reports_no_push_even_when_triggers_install(tmp_path) -> None:
    """The return value gates ChangeFeedService's LISTEN loop. SQLite has no
    NOTIFY, so it must report False and take the polled watch path."""

    async def scenario() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        await database.initialize()

        assert await install_change_tracking(database.engine) is False
        await database.shutdown()

    asyncio.run(scenario())


def test_missing_tracked_table_is_fatal(tmp_path) -> None:
    """An incomplete schema must not be mistaken for a healthy polling backend."""

    async def scenario() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        await database.initialize()

        with pytest.raises(RuntimeError, match="table_does_not_exist"):
            await install_change_tracking(database.engine, tables=("table_does_not_exist",))
        await database.shutdown()

    asyncio.run(scenario())
