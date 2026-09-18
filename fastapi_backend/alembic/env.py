"""Alembic environment for the OSCE AI Marker backend.

Two things make this file longer than the generated default:

1. The URL comes from the application's ``Settings``, not from ``alembic.ini``.
   The app resolves its database from ``APP_DATABASE_URL`` / ``DATABASE_URL`` /
   a default SQLite file under ``storage/``; duplicating that logic in an ini
   file is how a migration ends up applied to the wrong database.

2. Two tables in the database belong to nobody. ``app.database.models`` owns
   every table the application uses -- the jobs queue included, since the
   raw-SQL layer that used to own it was folded into the ORM. What is left is
   ``alembic_version`` (Alembic's own bookkeeping) and ``app_metadata``, a
   schema-version row the removed layer wrote on PostgreSQL. Neither is in
   ``target_metadata``, so autogenerate is told to ignore them rather than
   proposing to drop them on every run.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, event, pool


# fastapi_backend/ — the directory that makes "app" importable. prepend_sys_path
# in alembic.ini covers the CLI; this covers the app calling command.upgrade()
# from a different working directory.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import Settings  # noqa: E402
from app.database.models import Base  # noqa: E402
from app.database.schema_ownership import UNMANAGED_TABLES as SCHEMA_UNMANAGED_TABLES  # noqa: E402


config = context.config

# Only the CLI gets its logging configured from alembic.ini. When the app calls
# command.upgrade() at startup it sets ``configure_logger`` to False, because
# fileConfig() reconfigures the *root* logger — it would silently reset the log
# levels the application had already installed for itself.
#
# ``disable_existing_loggers=False`` matters even on the CLI path: the default
# switches off every logger not named in the ini file, which takes the whole
# ``app.*`` tree down with it.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


# Tables that exist in deployed databases but not in ``target_metadata``. See
# app/database/schema_ownership.py for what is on the list and why; it lives
# there so this file and the tests that pin the property read one value.
UNMANAGED_TABLES = SCHEMA_UNMANAGED_TABLES


def _sync_url() -> str:
    """The database URL with a *synchronous* driver.

    Alembic runs its migrations on a blocking connection. The app's engine uses
    ``sqlite+aiosqlite`` / ``postgresql+psycopg``; the first has no sync
    counterpart under that name, the second is already usable synchronously
    (psycopg 3 serves both).
    """
    override = context.get_x_argument(as_dictionary=True).get("db_url")
    if override:
        return override

    configured = config.get_main_option("sqlalchemy.url", None)
    if configured:
        return configured

    # Imported lazily-ish: OrmDatabase performs the same Path/URL normalisation
    # the app uses, so the two can never resolve to different files.
    from app.database.orm import OrmDatabase

    url = OrmDatabase._normalize_url(Settings.load().resolved_database_source)
    return to_sync_url(url)


def to_sync_url(url: str) -> str:
    """Strip the async driver from an app URL. Shared with the runtime runner."""
    if url.startswith("sqlite+aiosqlite://"):
        return url.replace("sqlite+aiosqlite://", "sqlite://", 1)
    return url


def _include_object(object_, name, type_, reflected, compare_to) -> bool:
    if type_ == "table" and name in UNMANAGED_TABLES:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=_include_object,
        compare_type=True,
        # SQLite cannot ALTER most things in place; batch mode rebuilds the
        # table instead. Harmless on PostgreSQL, essential on SQLite.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _check_sqlite_integrity(connection) -> None:
    violation = connection.exec_driver_sql("PRAGMA foreign_key_check").first()
    if violation is not None:
        raise RuntimeError(
            f"SQLite foreign-key violation in {violation[0]}; repair existing orphan rows before migrating."
        )


def _run_on_connection(connection) -> None:
    sqlite = connection.dialect.name == "sqlite"
    try:
        if sqlite:
            _check_sqlite_integrity(connection)
            connection.commit()
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=_include_object,
            compare_type=True,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
            if sqlite:
                _check_sqlite_integrity(connection)
        connection.commit()
    finally:
        connection.rollback()
        if sqlite:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()


def run_migrations_online() -> None:
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        _run_on_connection(supplied_connection)
        return

    from app.database.orm import OrmDatabase, configure_sqlite_connection

    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _sync_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=OrmDatabase._connect_args(section["sqlalchemy.url"]),
    )
    if connectable.dialect.name == "sqlite":
        event.listen(connectable, "connect", configure_sqlite_connection)
    try:
        with connectable.connect() as connection:
            _run_on_connection(connection)
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
