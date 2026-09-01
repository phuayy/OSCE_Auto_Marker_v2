"""Install the change-tracking triggers that maintain table_versions.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-01

This is the "versioning" half of what used to happen only when the app booted:
``install_change_tracking()`` in ``app/database/change_tracking.py`` attaches a
trigger per tracked table that bumps ``table_versions.version`` (and, on
PostgreSQL, fires ``pg_notify`` on the ``osce_changes`` channel) after every
committed write. The API's cached session index reads that counter to decide
whether its projection is stale, and the browser's change feed listens on the
channel.

Doing it here means a database provisioned with ``alembic upgrade head`` -- in
CI, or by an operator ahead of a deploy -- comes up with change tracking already
live, instead of depending on an API process having started at least once. The
startup call is kept: it is idempotent (every statement is DROP-then-CREATE) and
it is the only thing that can reinstall a trigger on a database an operator
restored from a schema-only dump.

The trigger bodies are frozen copies of the module's, not imports of it, for the
usual reason: a migration must keep meaning what it meant when it was written.
The two are expected to drift eventually -- when they do, the change belongs in
a *new* revision, and this one stays as it is.

Never fatal on failure. An untracked table's consumers fall back to polling,
which is exactly the behaviour that predates change tracking; refusing to
migrate over it would be a worse trade.
"""

from __future__ import annotations

import logging

from alembic import context, op
import sqlalchemy as sa


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


logger = logging.getLogger("alembic.runtime.migration")


CHANGE_CHANNEL = "osce_changes"
TRACKED_TABLES = ("sessions", "assessment_results", "jobs", "notifications")
_VERSION_TABLE = "table_versions"


# One *statement-level* trigger per table on PostgreSQL: a run that inserts 40
# assessment_criteria rows should announce one change, not forty. The counter
# bump and the notify happen in the same statement, and PostgreSQL holds the
# notification until COMMIT, so a listener never sees a change before the data
# behind it is visible.
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

# SQLite has neither statement-level triggers nor NOTIFY, so it degrades to
# row-level counter bumps: same correctness, poll-bound latency.
_SQLITE_BUMP = f"""
    INSERT INTO {_VERSION_TABLE} (table_name, version, updated_at)
    VALUES ('{{table}}', 1, CURRENT_TIMESTAMP)
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
            f"BEGIN {_SQLITE_BUMP.format(table=table)} END"
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
        # ``--sql`` emits a script rather than running one, so there is nothing
        # to isolate and nothing to inspect: write every statement out and let
        # whoever applies it deal with a table that is missing.
        if is_postgres:
            op.execute(sa.text(_PG_FUNCTION))
        for table in TRACKED_TABLES:
            for statement in _pg_statements(table) if is_postgres else _sqlite_statements(table):
                op.execute(sa.text(statement))
        return

    bind = op.get_bind()

    # Every group runs inside its own SAVEPOINT. Without one, a single failing
    # statement poisons the whole migration transaction on PostgreSQL, so one
    # absent table would take every *other* table's triggers down with it --
    # change tracking silently off wholesale, one log line as the only evidence.
    # This is the same isolation install_change_tracking() gets from using a
    # separate transaction per table.
    if is_postgres:
        # The shared function must exist before any trigger can reference it.
        # exec_driver_sql bypasses SQLAlchemy's bind-parameter parsing, which
        # would otherwise choke on plpgsql's dollar-quoting.
        try:
            with bind.begin_nested():
                bind.exec_driver_sql(_PG_FUNCTION)
        except Exception:
            logger.warning(
                "Could not create the change-tracking trigger function; "
                "consumers fall back to polled change detection.",
                exc_info=True,
            )
            return

    for table in TRACKED_TABLES:
        statements = _pg_statements(table) if is_postgres else _sqlite_statements(table)
        try:
            with bind.begin_nested():
                for statement in statements:
                    bind.exec_driver_sql(statement)
        except Exception as error:
            logger.warning(
                "Could not install change tracking on '%s' (%s); consumers of that "
                "table fall back to polling.",
                table,
                error,
            )


def downgrade() -> None:
    is_postgres = op.get_context().dialect.name == "postgresql"

    if context.is_offline_mode():
        for table in TRACKED_TABLES:
            for statement in _drop_statements(table, is_postgres=is_postgres):
                op.execute(sa.text(statement))
        if is_postgres:
            op.execute(sa.text("DROP FUNCTION IF EXISTS osce_bump_table_version()"))
        return

    bind = op.get_bind()

    for table in TRACKED_TABLES:
        for statement in _drop_statements(table, is_postgres=is_postgres):
            try:
                with bind.begin_nested():
                    bind.exec_driver_sql(statement)
            except Exception:
                # The table itself may already be gone; nothing left to detach.
                pass

    if is_postgres:
        op.execute(sa.text("DROP FUNCTION IF EXISTS osce_bump_table_version()"))
