"""Attach change tracking to provider_credentials and app_settings.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-12

Revision 0003 installed the trigger that bumps ``table_versions`` (and fires
``pg_notify``) for the tables whose writes invalidate a cached API projection.
These two were not on that list: ``provider_credentials`` did not exist yet, and
``app_settings`` was re-queried on every run instead of being cached.

Both are now cached in every process, which is only safe because of these
triggers. The scoring pipeline resolves a model selection and a set of API keys
before every assessment, in the API process and in the Hatchet worker alike;
those values change a handful of times a year. Without an announcement, a
process would have to choose between querying every run and never learning about
a change -- and "never learning" means a rotated key stays in use and a model
switched in the settings screen does not take effect until a restart.

The caches also compare the ``table_versions`` counter they were built from, so a
database that failed to install a trigger still converges: it just does so by
reading the counter rather than by being told. That is the fallback, not the
design.

The trigger bodies are frozen copies of ``app/database/change_tracking.py``'s,
not imports of it, for the same reason 0003 froze its own: a migration must keep
meaning what it meant when it was written. ``install_change_tracking()`` still
runs at boot and is idempotent (every statement is DROP-then-CREATE), so a
database that upgraded here and one that only ever booted the app end up
identical.

Never fatal on failure -- a table with no trigger falls back to counter
comparison, which is the behaviour that predates this revision.
"""

from __future__ import annotations

import logging

from alembic import context, op
import sqlalchemy as sa


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


logger = logging.getLogger("alembic.runtime.migration")


CHANGE_CHANNEL = "osce_changes"
_VERSION_TABLE = "table_versions"

# The tables this revision adds. Frozen: a later revision that tracks another
# table adds it there, never by editing this tuple.
_TABLES = ("provider_credentials", "app_settings")


# Statement-level on PostgreSQL, matching 0003. The shared function is created by
# 0003; re-created here with CREATE OR REPLACE so this revision also works on a
# database whose 0003 run could not install it.
_PG_FUNCTION = f"""
CREATE OR REPLACE FUNCTION osce_bump_table_version() RETURNS trigger AS $osce$
DECLARE
    next_version BIGINT;
BEGIN
    INSERT INTO {_VERSION_TABLE} (table_name, version, updated_at)
    VALUES (TG_TABLE_NAME, 1, now())
    ON CONFLICT (table_name)
    DO UPDATE SET version = {_VERSION_TABLE}.version + 1, updated_at = now()
    RETURNING version INTO next_version;

    PERFORM pg_notify(
        '{CHANGE_CHANNEL}',
        json_build_object('table', TG_TABLE_NAME, 'version', next_version, 'op', TG_OP)::text
    );
    RETURN NULL;
END;
$osce$ LANGUAGE plpgsql;
"""


def _sqlite_bump(table: str) -> str:
    return f"""
    INSERT INTO {_VERSION_TABLE} (table_name, version, updated_at)
    VALUES ('{table}', 1, CURRENT_TIMESTAMP)
    ON CONFLICT(table_name)
    DO UPDATE SET version = {_VERSION_TABLE}.version + 1, updated_at = CURRENT_TIMESTAMP;
"""


def _pg_statements(table: str) -> list[str]:
    trigger = f"trg_{table}_change"
    return [
        f"DROP TRIGGER IF EXISTS {trigger} ON {table}",
        (
            f"CREATE TRIGGER {trigger} "
            f"AFTER INSERT OR UPDATE OR DELETE ON {table} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION osce_bump_table_version()"
        ),
    ]


def _sqlite_statements(table: str) -> list[str]:
    statements: list[str] = []
    for operation in ("INSERT", "UPDATE", "DELETE"):
        trigger = f"trg_{table}_change_{operation.lower()}"
        statements.append(f"DROP TRIGGER IF EXISTS {trigger}")
        statements.append(
            f"CREATE TRIGGER {trigger} AFTER {operation} ON {table} "
            f"BEGIN {_sqlite_bump(table)} END"
        )
    return statements


def _drop_statements(table: str, *, is_postgres: bool) -> list[str]:
    if is_postgres:
        return [f"DROP TRIGGER IF EXISTS trg_{table}_change ON {table}"]
    return [
        f"DROP TRIGGER IF EXISTS trg_{table}_change_{operation}"
        for operation in ("insert", "update", "delete")
    ]


def upgrade() -> None:
    is_postgres = op.get_context().dialect.name == "postgresql"

    if context.is_offline_mode():
        # ``--sql`` emits a script rather than running one: write every statement
        # out and let whoever applies it deal with a missing table.
        if is_postgres:
            op.execute(sa.text(_PG_FUNCTION))
        for table in _TABLES:
            for statement in _pg_statements(table) if is_postgres else _sqlite_statements(table):
                op.execute(sa.text(statement))
        return

    bind = op.get_bind()

    # Every group runs inside its own SAVEPOINT, as in 0003. On PostgreSQL a
    # single failing statement otherwise poisons the whole migration
    # transaction, so one absent table would take the other's trigger with it.
    if is_postgres:
        try:
            with bind.begin_nested():
                # exec_driver_sql bypasses SQLAlchemy's bind-parameter parsing,
                # which would otherwise choke on plpgsql's dollar-quoting.
                bind.exec_driver_sql(_PG_FUNCTION)
        except Exception:
            logger.warning(
                "Could not create the change-tracking trigger function; the settings "
                "and credential caches fall back to counter comparison.",
                exc_info=True,
            )
            return

    for table in _TABLES:
        statements = _pg_statements(table) if is_postgres else _sqlite_statements(table)
        try:
            with bind.begin_nested():
                for statement in statements:
                    bind.exec_driver_sql(statement)
        except Exception as error:
            logger.warning(
                "Could not install change tracking on '%s' (%s); a change there will "
                "reach other processes on their next counter read instead of "
                "immediately.",
                table,
                error,
            )


def downgrade() -> None:
    is_postgres = op.get_context().dialect.name == "postgresql"

    if context.is_offline_mode():
        for table in _TABLES:
            for statement in _drop_statements(table, is_postgres=is_postgres):
                op.execute(sa.text(statement))
        return

    bind = op.get_bind()
    for table in _TABLES:
        for statement in _drop_statements(table, is_postgres=is_postgres):
            try:
                with bind.begin_nested():
                    bind.exec_driver_sql(statement)
            except Exception:
                # The table itself may already be gone; nothing left to detach.
                pass
