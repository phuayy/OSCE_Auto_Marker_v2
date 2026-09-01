from __future__ import annotations

import asyncio

from sqlalchemy import inspect, text

from app.database.migrations import ADDITIVE_MIGRATIONS, apply_additive_migrations
from app.database.orm import OrmDatabase


async def _columns(database: OrmDatabase, table: str) -> set[str]:
    def _read(connection) -> set[str]:
        return {column["name"] for column in inspect(connection).get_columns(table)}

    async with database.engine.connect() as connection:
        return await connection.run_sync(_read)


def test_missing_column_is_added_to_an_existing_database(tmp_path) -> None:
    """The scenario create_all cannot handle: a database that predates the column.

    Simulated by creating the schema, dropping the column back off, and letting
    the migration restore it — which is exactly what an upgraded install hits.
    """

    async def scenario() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        await database.initialize()

        async with database.engine.begin() as connection:
            await connection.exec_driver_sql("ALTER TABLE notifications DROP COLUMN event_type")
        assert "event_type" not in await _columns(database, "notifications")

        applied = await apply_additive_migrations(database.engine)

        assert "notifications.event_type" in applied
        assert "event_type" in await _columns(database, "notifications")
        await database.shutdown()

    asyncio.run(scenario())


def test_migration_is_idempotent(tmp_path) -> None:
    """Runs on every boot, so a second pass must be a no-op, not an error."""

    async def scenario() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        await database.initialize()

        # create_all already produced the column, so nothing is pending.
        assert await apply_additive_migrations(database.engine) == []
        assert await apply_additive_migrations(database.engine) == []
        await database.shutdown()

    asyncio.run(scenario())


def test_backfill_labels_rows_that_predate_the_column(tmp_path) -> None:
    """Legacy rows must not be left with a null type, or webhook filtering and
    the UI would treat historical notifications as untyped."""

    async def scenario() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        await database.initialize()

        async with database.engine.begin() as connection:
            await connection.exec_driver_sql("ALTER TABLE notifications DROP COLUMN event_type")
            await connection.exec_driver_sql(
                "INSERT INTO notifications (id, session_id, title, body, created_at) "
                "VALUES ('legacy-1', 's-1', 'Scoring complete', 'old row', CURRENT_TIMESTAMP)"
            )

        await apply_additive_migrations(database.engine)

        async with database.engine.connect() as connection:
            result = await connection.execute(
                text("SELECT event_type FROM notifications WHERE id = 'legacy-1'")
            )
            assert result.scalar_one() == "scoring.completed"
        await database.shutdown()

    asyncio.run(scenario())


def test_migrations_are_only_nullable_or_defaulted() -> None:
    """SQLite refuses ADD COLUMN NOT NULL without a default on a populated table,
    so an entry violating that would break every existing install."""
    for migration in ADDITIVE_MIGRATIONS:
        definition = migration.definition.upper()
        assert "NOT NULL" not in definition or "DEFAULT" in definition, migration
