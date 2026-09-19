"""Run the Alembic migrations from inside the application at startup.

The schema used to be produced entirely at boot: ``Base.metadata.create_all``
for the ORM tables, ``CREATE TABLE IF NOT EXISTS`` for the raw-SQL jobs tables,
``apply_additive_migrations`` for columns added after the fact, and
``install_change_tracking`` for the trigger-maintained version counters. That
works, but it has no record of *what* has been applied -- so nothing can be
rolled back, nothing can be reviewed in a diff, and a column drop or a type
change has no way to happen at all.

Alembic now owns the schema; this module is the bridge that keeps
``python scripts/run_api.py`` a single command rather than "remember to run
``alembic upgrade head`` first". Operators who prefer to migrate deliberately
can set ``DB_AUTO_MIGRATE=false`` and run the CLI themselves.

Three cases, distinguished by what is already in the database:

* **alembic_version present** -- an Alembic-managed database. Upgrade to head.
* **application tables present, no alembic_version** -- a database built by the
  old ``create_all`` path. Stamp it at ``0001`` (the baseline that describes
  what it already has) and then upgrade, so any revision after the baseline
  still gets applied. Stamping straight at ``head`` would skip them silently,
  which is the classic way a pre-Alembic database ends up permanently missing a
  column.
* **empty database** -- upgrade from scratch.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from threading import Lock

from sqlalchemy import create_engine, inspect
from sqlalchemy.ext.asyncio import AsyncEngine


_MIGRATION_LOCK = Lock()


logger = logging.getLogger(__name__)


# fastapi_backend/ — holds alembic.ini and the alembic/ script directory.
BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
ALEMBIC_SCRIPTS = BACKEND_ROOT / "alembic"

# The revision describing the schema the pre-Alembic startup path produced.
BASELINE_REVISION = "0001"

# Any one of these existing means the database predates Alembic rather than
# being empty. ``sessions`` is the table every deployment has.
_SENTINEL_TABLE = "sessions"
_VERSION_TABLE = "alembic_version"

# Postgres-only: a session-level advisory lock serializing "alembic upgrade
# head" across whatever reaches this database concurrently. `_MIGRATION_LOCK`
# above only serializes calls inside *this* process, and the single-API-
# instance file lock (app/core/single_instance.py) only serializes processes
# sharing one *local filesystem* storage root — neither stops two hosts that
# point at the same Postgres database but have their own storage root apiece
# (a rolling deploy, a blue/green swap, ALLOW_MULTIPLE_API_INSTANCES=true) from
# running Alembic at the same moment. A fixed, arbitrary constant identifies
# this application's migrations on the shared cluster; the lock is held for
# the DBAPI session's lifetime (not tied to any one transaction), so it
# survives the commits Alembic issues mid-upgrade and is released explicitly
# once the upgrade finishes either way.
_POSTGRES_MIGRATION_LOCK_KEY = 747_275_551_002


def to_sync_url(url: str) -> str:
    """Swap the async driver for its blocking equivalent.

    Alembic migrates over a synchronous connection. ``sqlite+aiosqlite`` has to
    become plain ``sqlite``; ``postgresql+psycopg`` needs no change, because
    psycopg 3 is both the sync and the async driver.
    """
    if url.startswith("sqlite+aiosqlite://"):
        return url.replace("sqlite+aiosqlite://", "sqlite://", 1)
    return url


def _build_config(sync_url: str):
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    # Absolute, because the app's working directory is the repository root while
    # alembic.ini's relative script_location is written for fastapi_backend/.
    config.set_main_option("script_location", str(ALEMBIC_SCRIPTS))
    config.set_main_option("sqlalchemy.url", sync_url.replace("%", "%%"))
    # Keep env.py's fileConfig() out of the way. It reconfigures the root logger
    # from alembic.ini, which would undo the levels and handlers the app has
    # already installed — and, with logging's default, switch off every logger
    # the ini file does not name.
    config.attributes["configure_logger"] = False
    return config


def _upgrade_connection(connection) -> str:
    is_postgres = connection.dialect.name == "postgresql"
    if is_postgres:
        connection.exec_driver_sql(f"SELECT pg_advisory_lock({_POSTGRES_MIGRATION_LOCK_KEY})")
    try:
        return _upgrade_connection_locked(connection)
    finally:
        if is_postgres:
            connection.exec_driver_sql(f"SELECT pg_advisory_unlock({_POSTGRES_MIGRATION_LOCK_KEY})")


def _upgrade_connection_locked(connection) -> str:
    from alembic import command

    config = _build_config(connection.engine.url.render_as_string(hide_password=False))
    config.attributes["connection"] = connection
    inspector = inspect(connection)
    has_version_table = inspector.has_table(_VERSION_TABLE)
    has_application_tables = inspector.has_table(_SENTINEL_TABLE)
    connection.commit()

    if not has_version_table and has_application_tables:
        logger.info(
            "Database predates Alembic; stamping it at the %s baseline before upgrading.",
            BASELINE_REVISION,
        )
        command.stamp(config, BASELINE_REVISION)
        action = "adopted"
    elif has_version_table:
        action = "upgraded"
    else:
        action = "created"

    command.upgrade(config, "head")
    return action


def _upgrade_sync(sync_url: str) -> str:
    from app.database.orm import OrmDatabase, configure_sqlite_connection
    from sqlalchemy import event

    engine = create_engine(sync_url, connect_args=OrmDatabase._connect_args(sync_url))
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", configure_sqlite_connection)
    try:
        with _MIGRATION_LOCK, engine.connect() as connection:
            return _upgrade_connection(connection)
    finally:
        engine.dispose()


async def migrate_engine(engine: AsyncEngine) -> str:
    """Reuse the engine's connection, serializing Alembic's process-global context.

    Nonblocking acquisition keeps async migrations and thread-based callers from
    blocking each other's event loops. The connection also preserves :memory: DBs.
    """
    while not _MIGRATION_LOCK.acquire(blocking=False):
        await asyncio.sleep(0.01)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(_upgrade_connection)
    finally:
        _MIGRATION_LOCK.release()


async def verify_database_revision(engine: AsyncEngine) -> None:
    from alembic.migration import MigrationContext
    from alembic.script import ScriptDirectory

    def verify(connection) -> None:
        expected = set(ScriptDirectory(str(ALEMBIC_SCRIPTS)).get_heads())
        actual = set(MigrationContext.configure(connection).get_current_heads())
        if actual != expected:
            raise RuntimeError("Database schema is not at Alembic head; run 'alembic upgrade head' before startup.")

    async with engine.connect() as connection:
        await connection.run_sync(verify)


async def run_database_migrations(database_source: Path | str | AsyncEngine) -> str:
    """Bring ``database_source`` up to the head revision.

    ``database_source`` is whatever ``Settings.resolved_database_source`` gives:
    a SQLite path or a PostgreSQL URL. It is normalised through ``OrmDatabase``
    so the migration and the application can never disagree about which database
    they mean.

    Returns the action taken (``"created"`` / ``"adopted"`` / ``"upgraded"``).
    Missing Alembic or a migration failure is fatal; there is no second schema
    writer to fall back to. Passing an engine preserves in-memory databases.
    """
    from app.database.orm import OrmDatabase

    if isinstance(database_source, AsyncEngine):
        action = await migrate_engine(database_source)
    else:
        sync_url = to_sync_url(OrmDatabase._normalize_url(database_source))
        action = await asyncio.to_thread(_upgrade_sync, sync_url)
    logger.info("Database schema %s via Alembic (head revision applied).", action)
    return action


__all__ = [
    "ALEMBIC_INI",
    "ALEMBIC_SCRIPTS",
    "BASELINE_REVISION",
    "migrate_engine",
    "run_database_migrations",
    "to_sync_url",
    "verify_database_revision",
]
