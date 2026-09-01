"""Trigger installation must degrade per table, not wholesale.

The triggers span two schema layers — ``sessions``/``assessment_results``/
``notifications`` come from the SQLAlchemy metadata, ``jobs`` from the raw-SQL
jobs schema — so a caller that has initialised only one layer is a real state,
not a hypothetical. Installing every table in one transaction meant that state
rolled back *all* the triggers, silently disabling push everywhere.
"""

from __future__ import annotations

import asyncio

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


def test_a_missing_table_does_not_disable_tracking_for_the_others(tmp_path) -> None:
    """The regression. Only the ORM layer is initialised here, so ``jobs`` does
    not exist — exactly the shape that previously wiped out every trigger."""

    async def scenario() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        await database.initialize()  # ORM tables only; no raw-SQL "jobs" table

        await install_change_tracking(database.engine)

        before = await _counter(database, "notifications")
        await _insert_notification(database, "n-1")
        assert await _counter(database, "notifications") > before, (
            "notifications tracking must survive the absence of an unrelated table"
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

        # "jobs" belongs to the other schema layer and is absent here.
        for table in TRACKED_TABLES:
            if table == "jobs":
                continue
            assert any(table in name for name in triggers), f"no trigger for {table}"
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


def test_no_tracked_tables_reports_no_push(tmp_path) -> None:
    """Nothing installed means nothing can announce; the caller must not be told
    push is available."""

    async def scenario() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        await database.initialize()

        assert await install_change_tracking(database.engine, tables=("table_does_not_exist",)) is False
        await database.shutdown()

    asyncio.run(scenario())
