"""Per-user preference overrides, and change tracking on the table.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-18

Until now the transcription engine, scoring model, marking mode and preprocess
toggle were one global document in ``app_settings`` — an operator's choice
applied to every marker's next run. Sharing the deployment between several
markers means each of them wants their own transcription engine and marking
method without touching anyone else's; the provider catalogue and the API keys
that authorise it stay genuinely shared, an admin's call.

One row per account, the whole overlay as one JSON document (``values``),
mirroring the shape ``app_settings`` already uses rather than inventing a
second one. Only the *user-scoped* key names
(``app.domain.settings_scope.USER_SCOPED_KEYS``) are ever written here; the
route enforces that boundary, not this table. Deleting the account takes its
overrides with it via ``ON DELETE CASCADE`` — there is nothing left for them to
mean.

Change tracking is attached in the same revision, as ``0005``/``0006``/``0008``
did for the other tables on the scoring hot path: a preference saved in one
process (the API) has to evict the cached copy in every other one (a Hatchet
worker) so it applies to that user's very next run, not their next restart.
Never fatal on failure — a table with no trigger falls back to counter
comparison, exactly as the other cached tables do.
"""

from __future__ import annotations

import logging

from alembic import context, op
import sqlalchemy as sa


revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


logger = logging.getLogger("alembic.runtime.migration")


CHANGE_CHANNEL = "osce_changes"
_VERSION_TABLE = "table_versions"
_USERS = "users"
_TABLE = "user_settings"


def _has_table(name: str) -> bool:
    """A database built by the pre-Alembic ``create_all`` path is stamped at
    0001 and then upgraded, so the ORM may already have created the table."""
    if context.is_offline_mode():
        return False
    return sa.inspect(op.get_bind()).has_table(name)


# Frozen copy of ``app/database/change_tracking.py``'s bodies, as in 0003,
# 0005, 0006 and 0008: a migration must keep meaning what it meant when
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
                "Could not create the change-tracking trigger function; user preferences "
                "fall back to counter comparison.",
                exc_info=True,
            )
            return

    try:
        with bind.begin_nested():
            for statement in _pg_statements() if is_postgres else _sqlite_statements():
                bind.exec_driver_sql(statement)
    except Exception as error:
        logger.warning(
            "Could not install change tracking on '%s' (%s); a saved preference will reach "
            "other processes on their next counter read instead of immediately.",
            _TABLE,
            error,
        )


def upgrade() -> None:
    if not _has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column(
                "user_id",
                sa.String(length=36),
                sa.ForeignKey(f"{_USERS}.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("values", sa.JSON(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

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
