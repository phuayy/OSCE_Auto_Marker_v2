"""Operator-defined scoring providers, and change tracking for them.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-12

Until now the set of scoring vendors was a property of the build: six modules in
``app/llm/registry.py``, and a seventh meant a release. This table makes it a
property of the deployment instead, so an institution that signs with a new
vendor, stands up a local vLLM box or is handed an Azure endpoint can configure
it in the settings screen and have the next assessment use it.

Two decisions are visible in the shape of the table.

**No API key column.** The credential goes to ``provider_credentials`` exactly
like a shipped provider's, so a custom vendor inherits the AES-256-GCM sealing,
the write-only API, the narrow subprocess forwarding and the
rotation-evicts-every-cache behaviour without a second implementation of any of
it. This table holds only the connection shape, which is why the API can return
it in full.

**One JSON column, not one column per field.** What a platform needs in order to
authenticate is a moving target - an auth header here, an API version query
parameter there, an account id in the URL - and a schema migration per field
would guarantee the product lags the market. Validation lives at the API
boundary in ``app/llm/custom.py``, where the error messages belong anyway. The
few fields that are also columns (label, base URL, format, enabled) are there so
an operator debugging an incident can see what a row points at with a plain
SELECT, and so ``enabled`` can be indexed.

The change-tracking trigger is the same one revisions 0003 and 0005 install, for
the same reason: the catalogue is resolved before every scoring run in every
process and is cached, and an edited endpoint that has not propagated means this
deployment's API key still going to the address the definition used to name.
Never fatal on failure - a table with no trigger falls back to counter
comparison.
"""

from __future__ import annotations

import logging

from alembic import context, op
import sqlalchemy as sa


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


logger = logging.getLogger("alembic.runtime.migration")


CHANGE_CHANNEL = "osce_changes"
_VERSION_TABLE = "table_versions"
_TABLE = "llm_providers"


def _has_table() -> bool:
    """Whether the table is already there.

    A database built by the pre-Alembic ``create_all`` path is stamped at 0001
    and then upgraded, so by the time this revision runs the ORM may already
    have created the table. Creating it again would abort the upgrade; the
    trigger install below is idempotent and still runs either way.
    """
    if context.is_offline_mode():
        # ``--sql`` has no database to interrogate; assume the fresh-install
        # case the generated script is for.
        return False
    return sa.inspect(op.get_bind()).has_table(_TABLE)


# Frozen copies of ``app/database/change_tracking.py``'s bodies, as in 0003 and
# 0005: a migration must keep meaning what it meant when it was written.
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
                # exec_driver_sql bypasses SQLAlchemy's bind-parameter parsing,
                # which would otherwise choke on plpgsql's dollar-quoting.
                bind.exec_driver_sql(_PG_FUNCTION)
        except Exception:
            logger.warning(
                "Could not create the change-tracking trigger function; the custom provider "
                "catalogue falls back to counter comparison.",
                exc_info=True,
            )
            return

    try:
        with bind.begin_nested():
            for statement in _pg_statements() if is_postgres else _sqlite_statements():
                bind.exec_driver_sql(statement)
    except Exception as error:
        logger.warning(
            "Could not install change tracking on '%s' (%s); an edit there will reach other "
            "processes on their next counter read instead of immediately.",
            _TABLE,
            error,
        )


def upgrade() -> None:
    if _has_table():
        _install_change_tracking()
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("vendor", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("base_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("api_format", sa.String(length=32), nullable=False, server_default="openai"),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(length=120), nullable=True),
    )
    op.create_index("idx_llm_providers_enabled", _TABLE, ["enabled"])
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
                # The table itself may already be gone; nothing left to detach.
                pass

    if context.is_offline_mode() or _has_table():
        op.drop_index("idx_llm_providers_enabled", table_name=_TABLE)
        op.drop_table(_TABLE)
