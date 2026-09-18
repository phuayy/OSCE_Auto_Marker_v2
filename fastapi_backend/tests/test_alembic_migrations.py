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


# The queue's tables, named here because they are the ones that used to live
# outside the ORM metadata — described by hand in a raw-SQL module with a
# connection pool of their own. They are models now, so "alembic upgrade head"
# and Base.metadata describe one and the same schema, and `alembic check` can
# see drift in this half like any other.
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
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
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
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
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


def test_the_jobs_tables_are_managed_by_alembic() -> None:
    """The env's ignore list is what made the second schema source invisible to
    autogenerate. With the raw layer gone, only Alembic's own bookkeeping and a
    dead ``app_metadata`` row may stay outside the metadata."""
    from app.database.schema_ownership import UNMANAGED_TABLES

    assert UNMANAGED_TABLES.isdisjoint(JOBS_TABLES)
    assert UNMANAGED_TABLES == frozenset({"app_metadata", "alembic_version"})


def test_runtime_connections_enforce_sqlite_pragmas(tmp_path) -> None:
    async def scenario() -> None:
        database = OrmDatabase(tmp_path / "pragmas.sqlite3")
        await database.initialize()
        async with database.engine.connect() as first, database.engine.connect() as second:
            for connection in (first, second):
                for pragma, expected in (("journal_mode", "wal"), ("synchronous", 1), ("foreign_keys", 1)):
                    assert (await connection.exec_driver_sql(f"PRAGMA {pragma}")).scalar_one() == expected
        await database.engine.dispose()
        async with database.engine.connect() as connection:
            assert (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one() == 1
        await database.shutdown()

    asyncio.run(scenario())


def test_initialization_uses_alembic_including_in_memory_databases() -> None:
    async def scenario() -> None:
        database = OrmDatabase("sqlite+aiosqlite:///:memory:")
        await asyncio.gather(database.initialize(), database.initialize())
        async with database.engine.connect() as connection:
            assert (await connection.exec_driver_sql("SELECT version_num FROM alembic_version")).scalar_one() == HEAD_REVISION
            assert (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one() == 1
        await database.shutdown()

    asyncio.run(scenario())


def test_bootstrap_and_cli_produce_identical_schema(tmp_path) -> None:
    from alembic import command
    from app.database.migration_runner import _build_config
    from app.database.migrations import apply_additive_migrations

    def snapshot(connection):
        inspector = inspect(connection)
        return {
            table: {
                "columns": [tuple(row) for row in connection.exec_driver_sql(f'PRAGMA table_info("{table}")')],
                "indexes": inspector.get_indexes(table),
                "foreign_keys": inspector.get_foreign_keys(table),
            }
            for table in inspector.get_table_names()
        }, connection.exec_driver_sql("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name").all()

    async def scenario() -> None:
        bootstrap = OrmDatabase(tmp_path / "bootstrap.sqlite3")
        direct = OrmDatabase(tmp_path / "direct.sqlite3")
        await apply_additive_migrations(bootstrap.engine)
        await asyncio.to_thread(command.upgrade, _build_config(to_sync_url(direct.url)), "head")
        async with bootstrap.engine.connect() as left, direct.engine.connect() as right:
            assert await left.run_sync(snapshot) == await right.run_sync(snapshot)
        await asyncio.to_thread(command.check, _build_config(to_sync_url(direct.url)))
        await bootstrap.shutdown()
        await direct.shutdown()

    asyncio.run(scenario())


def test_foreign_keys_reject_orphans_and_cascade_job_children(tmp_path) -> None:
    from sqlalchemy.exc import IntegrityError

    async def scenario() -> None:
        database = OrmDatabase(tmp_path / "fk.sqlite3")
        await database.initialize()
        async with database.engine.begin() as connection:
            with pytest.raises(IntegrityError):
                await connection.exec_driver_sql("INSERT INTO job_events (job_id, event_type, created_at) VALUES ('missing', 'test', 'now')")
            await connection.exec_driver_sql("INSERT INTO jobs (id, session_id, task_type, status, created_at, updated_at) VALUES ('j1', 's1', 'test', 'queued', 'now', 'now')")
            await connection.exec_driver_sql("INSERT INTO job_events (job_id, event_type, created_at) VALUES ('j1', 'test', 'now')")
            await connection.exec_driver_sql("INSERT INTO job_attempts (job_id, attempt_number, status, started_at) VALUES ('j1', 1, 'running', 'now')")
            await connection.exec_driver_sql("DELETE FROM jobs WHERE id = 'j1'")
            for table in ("job_events", "job_attempts"):
                assert (await connection.exec_driver_sql(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 0
        await database.shutdown()

    asyncio.run(scenario())


def test_upgrade_preserves_job_history_and_restores_tracking(tmp_path) -> None:
    from alembic import command
    from app.database.migration_runner import _build_config
    from app.database.migrations import apply_additive_migrations
    from app.database.change_tracking import verify_change_tracking

    path = tmp_path / "old.sqlite3"
    command.upgrade(_build_config(_sync_url(path)), "0006")
    engine = create_engine(_sync_url(path))
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO jobs (id, session_id, task_type, status, created_at, updated_at) VALUES ('j1', 's1', 'test', 'queued', 'now', 'now')")
            connection.exec_driver_sql("INSERT INTO job_events (job_id, event_type, created_at) VALUES ('j1', 'test', 'now')")
            connection.exec_driver_sql("INSERT INTO job_attempts (job_id, attempt_number, status, started_at) VALUES ('j1', 1, 'running', 'now')")
    finally:
        engine.dispose()

    async def scenario() -> None:
        database = OrmDatabase(path)
        await apply_additive_migrations(database.engine)
        await verify_change_tracking(database.engine)
        async with database.engine.begin() as connection:
            for table in ("jobs", "job_events", "job_attempts"):
                assert (await connection.exec_driver_sql(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 1
            assert (await connection.exec_driver_sql("PRAGMA foreign_key_check")).first() is None
            before = (await connection.exec_driver_sql("SELECT version FROM table_versions WHERE table_name = 'jobs'")).scalar_one()
            await connection.exec_driver_sql("UPDATE jobs SET status = 'running' WHERE id = 'j1'")
            after = (await connection.exec_driver_sql("SELECT version FROM table_versions WHERE table_name = 'jobs'")).scalar_one()
            assert after == before + 1
            assert (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one() == 1
        await database.shutdown()

    asyncio.run(scenario())


def test_existing_orphans_fail_upgrade_without_deleting_data(tmp_path) -> None:
    from alembic import command
    from app.database.migration_runner import _build_config
    from app.database.migrations import apply_additive_migrations

    path = tmp_path / "orphan.sqlite3"
    command.upgrade(_build_config(_sync_url(path)), "0009")
    engine = create_engine(_sync_url(path))
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO job_events (job_id, event_type, created_at) VALUES ('missing', 'test', 'now')")
    finally:
        engine.dispose()

    async def scenario() -> None:
        database = OrmDatabase(path)
        with pytest.raises(RuntimeError, match="foreign-key violation in job_events"):
            await apply_additive_migrations(database.engine)
        async with database.engine.connect() as connection:
            assert (await connection.exec_driver_sql("SELECT COUNT(*) FROM job_events")).scalar_one() == 1
            assert (await connection.exec_driver_sql("SELECT version_num FROM alembic_version")).scalar_one() == "0009"
            assert (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one() == 1
        await database.shutdown()

    asyncio.run(scenario())


def test_failed_migration_rolls_back_ddl_and_restores_foreign_keys(tmp_path) -> None:
    from alembic import command
    from sqlalchemy import event
    from app.database.migration_runner import _build_config
    from app.database.migrations import apply_additive_migrations

    path = tmp_path / "failed.sqlite3"
    command.upgrade(_build_config(_sync_url(path)), "0009")

    def reject_trigger(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("CREATE TRIGGER trg_users_change_update"):
            raise RuntimeError("trigger installation rejected")

    async def scenario() -> None:
        database = OrmDatabase(path)
        query = "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        async with database.engine.connect() as connection:
            before = (await connection.exec_driver_sql(query)).all()
        event.listen(database.engine.sync_engine, "before_cursor_execute", reject_trigger)
        try:
            with pytest.raises(RuntimeError, match="trigger installation rejected"):
                await apply_additive_migrations(database.engine)
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", reject_trigger)
        async with database.engine.connect() as connection:
            assert (await connection.exec_driver_sql(query)).all() == before
            assert (await connection.exec_driver_sql("SELECT version_num FROM alembic_version")).scalar_one() == "0009"
            assert (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one() == 1
        assert await apply_additive_migrations(database.engine) == "upgraded"
        await database.shutdown()

    asyncio.run(scenario())


def test_wal_writer_commits_while_a_reader_holds_a_snapshot(tmp_path) -> None:
    async def scenario() -> None:
        database = OrmDatabase(tmp_path / "concurrent.sqlite3")
        await database.initialize()
        async with database.engine.begin() as connection:
            await connection.exec_driver_sql("INSERT INTO app_settings (key, value, updated_at) VALUES ('test', 'before', CURRENT_TIMESTAMP)")
        async with database.engine.connect() as reader, database.engine.connect() as writer:
            await reader.exec_driver_sql("BEGIN")
            query = "SELECT value FROM app_settings WHERE key = 'test'"
            assert (await reader.exec_driver_sql(query)).scalar_one() == "before"
            await writer.exec_driver_sql("UPDATE app_settings SET value = 'after' WHERE key = 'test'")
            await asyncio.wait_for(writer.commit(), timeout=2)
            assert (await reader.exec_driver_sql(query)).scalar_one() == "before"
            await reader.commit()
            assert (await reader.exec_driver_sql(query)).scalar_one() == "after"
        await database.shutdown()

    asyncio.run(scenario())


def test_postgres_bootstrap_matches_cli_and_tracking_is_enforced() -> None:
    import os
    from uuid import uuid4

    from alembic import command
    from sqlalchemy.engine import make_url

    from app.database.change_tracking import verify_change_tracking
    from app.database.migration_runner import _build_config
    from app.database.migrations import apply_additive_migrations

    url = os.environ.get("OSCE_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("OSCE_TEST_POSTGRES_URL must point to a disposable PostgreSQL database")
    schemas = [f"osce_test_{uuid4().hex}" for _ in range(2)]
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            for schema in schemas:
                connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    finally:
        engine.dispose()
    urls = [make_url(url).update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False) for schema in schemas]

    def snapshot(connection):
        schema = connection.exec_driver_sql("SELECT current_schema()").scalar_one()
        inspector = inspect(connection)
        columns = connection.exec_driver_sql(
            "SELECT c.relname, a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod), "
            "a.attnotnull, pg_catalog.pg_get_expr(d.adbin, d.adrelid) "
            "FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum "
            "WHERE n.nspname = current_schema() AND c.relkind = 'r' "
            "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY c.relname, a.attnum"
        ).all()
        indexes = {table: inspector.get_indexes(table) for table in inspector.get_table_names()}
        foreign_keys = {table: inspector.get_foreign_keys(table) for table in inspector.get_table_names()}
        return repr((columns, indexes, foreign_keys)).replace(schema, "test_schema")

    async def scenario() -> None:
        bootstrap, direct = (OrmDatabase(value) for value in urls)
        try:
            await apply_additive_migrations(bootstrap.engine)
            await asyncio.to_thread(command.upgrade, _build_config(urls[1]), "head")
            async with bootstrap.engine.connect() as left, direct.engine.connect() as right:
                assert await left.run_sync(snapshot) == await right.run_sync(snapshot)
            await asyncio.to_thread(command.check, _build_config(urls[1]))
            assert await verify_change_tracking(bootstrap.engine) is True
            assert await verify_change_tracking(direct.engine) is True
            async with bootstrap.engine.begin() as connection:
                await connection.exec_driver_sql("ALTER TABLE users DISABLE TRIGGER trg_users_change")
            with pytest.raises(RuntimeError, match="trg_users_change"):
                await verify_change_tracking(bootstrap.engine)
        finally:
            await bootstrap.shutdown()
            await direct.shutdown()

    asyncio.run(scenario())


def test_every_application_table_is_in_the_orm_metadata() -> None:
    from app.database.models import Base

    for table in JOBS_TABLES:
        assert table in Base.metadata.tables
