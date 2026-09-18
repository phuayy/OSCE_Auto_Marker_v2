"""Pin SQLite connection policy and restore complete change tracking.

Revision ID: 0010
Revises: 0009

Runtime and online Alembic connections use WAL, synchronous=NORMAL and foreign
keys. WAL persists in file databases; synchronous and foreign_keys are per
connection and must be set by the connection listener, not just a revision.
In-memory SQLite retains journal_mode=memory. NORMAL trades the most recent
commits on power loss for fewer syncs; it does not weaken foreign-key checks.
WAL needs local storage with shared-memory locking, not a network filesystem.
PostgreSQL retains its native durability and foreign-key enforcement unchanged.

Alembic disables SQLite foreign keys only around transactional batch rebuilds,
checking integrity before and after and restoring enforcement before returning
the connection. Otherwise revision 0007 can cascade-delete job history while
replacing jobs. It also drops that table's triggers; restore all tracking here,
including any earlier installation that was allowed to fail silently. Failures
in this revision are fatal. Startup only validates, never repairs, the schema.

Downgrading does not weaken connection policy or remove repaired triggers:
these are safety invariants, not schema objects introduced at this revision.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_TRACKED_TABLES = (
    "sessions", "assessment_results", "jobs", "notifications",
    "provider_credentials", "app_settings", "llm_providers", "users",
)

_PG_FUNCTION = """
CREATE OR REPLACE FUNCTION osce_bump_table_version() RETURNS trigger AS $osce$
DECLARE
    next_version BIGINT;
BEGIN
    INSERT INTO table_versions (table_name, version, updated_at)
    VALUES (TG_TABLE_NAME, 1, now())
    ON CONFLICT (table_name)
    DO UPDATE SET version = table_versions.version + 1, updated_at = now()
    RETURNING version INTO next_version;
    PERFORM pg_notify(
        'osce_changes',
        json_build_object('table', TG_TABLE_NAME, 'version', next_version, 'op', TG_OP)::text
    );
    RETURN NULL;
END;
$osce$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(_PG_FUNCTION))
        for table in _TRACKED_TABLES:
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{table}_change ON {table}"))
            op.execute(sa.text(
                f"CREATE TRIGGER trg_{table}_change AFTER INSERT OR UPDATE OR DELETE ON {table} "
                "FOR EACH STATEMENT EXECUTE FUNCTION osce_bump_table_version()"
            ))
    elif dialect == "sqlite":
        for table in _TRACKED_TABLES:
            for operation in ("INSERT", "UPDATE", "DELETE"):
                trigger = f"trg_{table}_change_{operation.lower()}"
                op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger}"))
                op.execute(sa.text(
                    f"CREATE TRIGGER {trigger} AFTER {operation} ON {table} BEGIN "
                    "INSERT INTO table_versions (table_name, version, updated_at) "
                    f"VALUES ('{table}', 1, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(table_name) DO UPDATE SET "
                    "version = table_versions.version + 1, updated_at = CURRENT_TIMESTAMP; END"
                ))
    else:
        raise RuntimeError(f"Unsupported change-tracking dialect: {dialect}")


def downgrade() -> None:
    pass
