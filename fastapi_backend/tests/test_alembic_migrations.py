from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine, inspect, text

from app.database.migration_runner import (
    BASELINE_REVISION,
    run_database_migrations,
    to_sync_url,
)
from app.database.models import Base
from app.database.orm import OrmDatabase


pytest.importorskip("alembic", reason="Alembic is an optional install for the schema tooling.")


# Tables Alembic must produce. The ORM half comes from the metadata; the jobs
# half is owned by the raw-SQL layer and created by revision 0001 as well, so a
# database built purely by "alembic upgrade head" is complete.
JOBS_TABLES = {"jobs", "job_attempts", "job_events"}


def _head_revision() -> str:
    """Whatever the versions directory currently tops out at.

    Read from the scripts rather than pinned to a literal: these tests assert
    that a migrated database lands on *head*, which is a property that must keep
    holding as revisions are added, not a claim about which revision is newest.
    """
    from alembic.script import ScriptDirectory

    from app.database.migration_runner import ALEMBIC_SCRIPTS

    return ScriptDirectory(str(ALEMBIC_SCRIPTS)).get_current_head()


HEAD_REVISION = _head_revision()


def _sync_url(path) -> str:
    return to_sync_url(OrmDatabase._normalize_url(path))


def _tables(path) -> set[str]:
    engine = create_engine(_sync_url(path), future=True)
    try:
        with engine.connect() as connection:
            return set(inspect(connection).get_table_names())
    finally:
        engine.dispose()


def _stamped_revision(path) -> str | None:
    engine = create_engine(_sync_url(path), future=True)
    try:
        with engine.connect() as connection:
            if not inspect(connection).has_table("alembic_version"):
                return None
            return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    finally:
        engine.dispose()


def test_upgrade_builds_the_whole_schema_from_empty(tmp_path) -> None:
    """The point of the migrations: a usable database without booting the app."""
    database_path = tmp_path / "app.sqlite3"

    action = asyncio.run(run_database_migrations(database_path))

    assert action == "created"
    tables = _tables(database_path)
    assert set(Base.metadata.tables) <= tables
    assert JOBS_TABLES <= tables
    assert _stamped_revision(database_path) == HEAD_REVISION


def test_event_type_column_and_backfill_are_applied(tmp_path) -> None:
    """Revision 0002 must reach a database it built itself, not just a legacy one."""
    database_path = tmp_path / "app.sqlite3"
    asyncio.run(run_database_migrations(database_path))

    engine = create_engine(_sync_url(database_path), future=True)
    try:
        with engine.connect() as connection:
            columns = {column["name"] for column in inspect(connection).get_columns("notifications")}
    finally:
        engine.dispose()

    assert "event_type" in columns


def test_change_tracking_triggers_are_installed(tmp_path) -> None:
    """Revision 0003 carries the versioning that used to need an app boot."""
    database_path = tmp_path / "app.sqlite3"
    asyncio.run(run_database_migrations(database_path))

    engine = create_engine(_sync_url(database_path), future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO sessions (id, status, payload, created_at, updated_at) "
                    "VALUES ('s-1', 'uploaded', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
        with engine.connect() as connection:
            version = connection.execute(
                text("SELECT version FROM table_versions WHERE table_name = 'sessions'")
            ).scalar_one()
    finally:
        engine.dispose()

    assert version == 1


def test_rerunning_upgrade_is_a_no_op(tmp_path) -> None:
    """Startup runs this on every boot, so a second pass must not error."""
    database_path = tmp_path / "app.sqlite3"

    assert asyncio.run(run_database_migrations(database_path)) == "created"
    assert asyncio.run(run_database_migrations(database_path)) == "upgraded"
    assert _stamped_revision(database_path) == HEAD_REVISION


def test_pre_alembic_database_is_adopted_and_then_upgraded(tmp_path) -> None:
    """The upgrade path that matters most: an install created by create_all.

    Such a database has every table (including ``notifications.event_type``) but
    no ``alembic_version``. It must be stamped at the baseline rather than at
    head, so post-baseline revisions still run — and 0002 must tolerate the
    column already being there.
    """
    database_path = tmp_path / "app.sqlite3"

    async def build_legacy_database() -> None:
        database = OrmDatabase(database_path)
        await database.initialize()
        await database.shutdown()

    asyncio.run(build_legacy_database())
    assert _stamped_revision(database_path) is None

    action = asyncio.run(run_database_migrations(database_path))

    assert action == "adopted"
    assert _stamped_revision(database_path) == HEAD_REVISION


def test_legacy_rows_are_backfilled_when_a_database_is_adopted(tmp_path) -> None:
    """A database that predates event_type entirely: 0002 has to add *and* fill."""
    database_path = tmp_path / "app.sqlite3"

    async def build_database_without_the_column() -> None:
        database = OrmDatabase(database_path)
        await database.initialize()
        async with database.engine.begin() as connection:
            await connection.exec_driver_sql(
                "ALTER TABLE notifications DROP COLUMN event_type"
            )
            await connection.exec_driver_sql(
                "INSERT INTO notifications (id, session_id, title, body, created_at) "
                "VALUES ('legacy-1', 's-1', 'Scoring complete', 'old row', CURRENT_TIMESTAMP)"
            )
        await database.shutdown()

    asyncio.run(build_database_without_the_column())
    asyncio.run(run_database_migrations(database_path))

    engine = create_engine(_sync_url(database_path), future=True)
    try:
        with engine.connect() as connection:
            event_type = connection.execute(
                text("SELECT event_type FROM notifications WHERE id = 'legacy-1'")
            ).scalar_one()
    finally:
        engine.dispose()

    assert event_type == "scoring.completed"


def test_baseline_revision_is_the_one_a_legacy_database_gets_stamped_at() -> None:
    """Stamping at head instead would silently skip every later revision."""
    assert BASELINE_REVISION == "0001"


def test_sqlite_async_driver_is_stripped_for_the_migration_connection() -> None:
    """Alembic migrates on a blocking connection; aiosqlite cannot serve one."""
    assert to_sync_url("sqlite+aiosqlite:///C:/tmp/app.sqlite3") == "sqlite:///C:/tmp/app.sqlite3"
    # psycopg 3 is both the sync and the async driver, so this URL is unchanged.
    assert (
        to_sync_url("postgresql+psycopg://user:pass@host/db")
        == "postgresql+psycopg://user:pass@host/db"
    )
