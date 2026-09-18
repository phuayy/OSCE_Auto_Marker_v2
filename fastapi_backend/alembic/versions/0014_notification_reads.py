"""Per-viewer notification read state, and change tracking on the table.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-18

``notifications.read_at`` was a single column on the shared row, so
``mark_all_read`` was one UPDATE with no viewer scoping: any marker dismissing
their feed cleared the unread badge for every marker on the deployment.
Notifications themselves stay team-wide by design (every marker sees every
session, so every marker sees every notification) — only *read state* needed
to become personal. One row per (notification, viewer) rather than widening
``notifications`` itself, mirroring how ``user_settings`` (0011) sits beside
``app_settings`` instead of adding per-user columns to a shared table.

Change tracking is attached in the same revision, as 0005/0006/0008/0011 did:
a mark-read in one process (or the Hatchet worker, for a notification it
raised itself) must evict that viewer's cached feed in every other process.
Never fatal on failure — a table with no trigger falls back to counter
comparison, exactly as the other cached tables do.
"""

from __future__ import annotations

import logging

from alembic import context, op
import sqlalchemy as sa


revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


logger = logging.getLogger("alembic.runtime.migration")


CHANGE_CHANNEL = "osce_changes"
_VERSION_TABLE = "table_versions"
_NOTIFICATIONS = "notifications"
_USERS = "users"
_TABLE = "notification_reads"


def _has_table(name: str) -> bool:
    """A database built by the pre-Alembic ``create_all`` path is stamped at
    0001 and then upgraded, so the ORM may already have created the table."""
    if context.is_offline_mode():
        return False
    return sa.inspect(op.get_bind()).has_table(name)


# Frozen copy of ``app/database/change_tracking.py``'s bodies, as in 0003,
# 0005, 0006, 0008 and 0011: a migration must keep meaning what it meant when
# written, even if the live trigger SQL changes later.
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


def _sqlite_bump() -> str:
    return f"""
    INSERT INTO {_VERSION_TABLE} (table_name, version, updated_at)
    VALUES ('{_TABLE}', 1, CURRENT_TIMESTAMP)
    ON CONFLICT(table_name)
    DO UPDATE SET version = {_VERSION_TABLE}.version + 1, updated_at = CURRENT_TIMESTAMP;
"""


def _pg_statements() -> list[str]:
    trigger = f"trg_{_TABLE}_change"
    return [
        f"DROP TRIGGER IF EXISTS {trigger} ON {_TABLE}",
        (
            f"CREATE TRIGGER {trigger} "
            f"AFTER INSERT OR UPDATE OR DELETE ON {_TABLE} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION osce_bump_table_version()"
        ),
    ]


def _sqlite_statements() -> list[str]:
    statements: list[str] = []
    for operation in ("INSERT", "UPDATE", "DELETE"):
        trigger = f"trg_{_TABLE}_change_{operation.lower()}"
        statements.append(f"DROP TRIGGER IF EXISTS {trigger}")
        statements.append(
            f"CREATE TRIGGER {trigger} AFTER {operation} ON {_TABLE} BEGIN {_sqlite_bump()} END"
        )
    return statements


def _install_change_tracking() -> None:
    is_postgres = op.get_context().dialect.name == "postgresql"

    if context.is_offline_mode():
        if is_postgres:
            op.execute(sa.text(_PG_FUNCTION))
        for statement in _pg_statements() if is_postgres else _sqlite_statements():
            op.execute(sa.text(statement))
        return

    bind = op.get_bind()
    if is_postgres:
        try:
            with bind.begin_nested():
                bind.exec_driver_sql(_PG_FUNCTION)
        except Exception:
            logger.warning(
                "Could not create the change-tracking trigger function; notification read "
                "state falls back to counter comparison.",
                exc_info=True,
            )
            return

    try:
        with bind.begin_nested():
            for statement in _pg_statements() if is_postgres else _sqlite_statements():
                bind.exec_driver_sql(statement)
    except Exception as error:
        logger.warning(
            "Could not install change tracking on '%s' (%s); a mark-read will reach other "
            "processes on their next counter read instead of immediately.",
            _TABLE,
            error,
        )


def upgrade() -> None:
    if not _has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column(
                "notification_id",
                sa.String(length=36),
                sa.ForeignKey(f"{_NOTIFICATIONS}.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "user_id",
                sa.String(length=36),
                sa.ForeignKey(f"{_USERS}.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("read_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(f"idx_{_TABLE}_user", _TABLE, ["user_id"], if_not_exists=True)

    _install_change_tracking()


def downgrade() -> None:
    is_postgres = op.get_context().dialect.name == "postgresql"
    drops = (
        [f"DROP TRIGGER IF EXISTS trg_{_TABLE}_change ON {_TABLE}"]
        if is_postgres
        else [
            f"DROP TRIGGER IF EXISTS trg_{_TABLE}_change_{operation}"
            for operation in ("insert", "update", "delete")
        ]
    )
    if context.is_offline_mode():
        for statement in drops:
            op.execute(sa.text(statement))
    else:
        bind = op.get_bind()
        for statement in drops:
            try:
                with bind.begin_nested():
                    bind.exec_driver_sql(statement)
            except Exception:
                pass

    if context.is_offline_mode() or _has_table(_TABLE):
        op.drop_table(_TABLE)
