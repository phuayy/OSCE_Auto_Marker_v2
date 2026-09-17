"""User accounts, the emailed action tokens that activate them, and change
tracking on the accounts table.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-17

Until now the deployment had one account, described by a JSON file beside the
database (``storage/auth/credentials.json``) and known to nothing else. Sharing
the app needs accounts that can be added and removed by an administrator
without anyone touching the server, so accounts become rows.

Two tables. ``users`` is the account: role, status, the bcrypt hash, and
``token_version`` — the integer a bearer token has to match on every request,
which is how disabling an account or changing its password revokes the tokens
it already holds. ``user_action_tokens`` is the emailed capability — accept an
invitation, reset a password — stored as a SHA-256 of the token so a database
dump cannot be replayed as a link. The token itself is 256 bits from a CSPRNG,
which is why a fast hash is enough.

Change tracking is attached to ``users`` in the same revision, as ``0005`` did
for the credentials and ``0006`` for the provider catalogue, and for the same
reason: the row is consulted on every authenticated request, so it is cached in
every API process, and an admin's "disable" has to evict those copies at once.
Never fatal on failure — a table with no trigger falls back to counter
comparison, exactly as the other cached tables do.

The bootstrap admin is *not* seeded here. Seeding needs the legacy credential
file or the ``DEFAULT_ADMIN_PASSWORD`` environment, neither of which a migration
should read; ``UserAdminService.ensure_bootstrap_admin`` does it at startup, in
the API role only.
"""

from __future__ import annotations

import logging

from alembic import context, op
import sqlalchemy as sa


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


logger = logging.getLogger("alembic.runtime.migration")


CHANGE_CHANNEL = "osce_changes"
_VERSION_TABLE = "table_versions"
_USERS = "users"
_TOKENS = "user_action_tokens"


def _has_table(name: str) -> bool:
    """A database built by the pre-Alembic ``create_all`` path is stamped at
    0001 and then upgraded, so the ORM may already have created the table."""
    if context.is_offline_mode():
        return False
    return sa.inspect(op.get_bind()).has_table(name)


# Frozen copies of ``app/database/change_tracking.py``'s bodies, as in 0003,
# 0005 and 0006: a migration must keep meaning what it meant when written.
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
    VALUES ('{_USERS}', 1, CURRENT_TIMESTAMP)
    ON CONFLICT(table_name)
    DO UPDATE SET version = {_VERSION_TABLE}.version + 1, updated_at = CURRENT_TIMESTAMP;
"""


def _pg_statements() -> list[str]:
    trigger = f"trg_{_USERS}_change"
    return [
        f"DROP TRIGGER IF EXISTS {trigger} ON {_USERS}",
        (
            f"CREATE TRIGGER {trigger} "
            f"AFTER INSERT OR UPDATE OR DELETE ON {_USERS} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION osce_bump_table_version()"
        ),
    ]


def _sqlite_statements() -> list[str]:
    statements: list[str] = []
    for operation in ("INSERT", "UPDATE", "DELETE"):
        trigger = f"trg_{_USERS}_change_{operation.lower()}"
        statements.append(f"DROP TRIGGER IF EXISTS {trigger}")
        statements.append(
            f"CREATE TRIGGER {trigger} AFTER {operation} ON {_USERS} BEGIN {_sqlite_bump()} END"
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
                "Could not create the change-tracking trigger function; the user directory "
                "falls back to counter comparison.",
                exc_info=True,
            )
            return

    try:
        with bind.begin_nested():
            for statement in _pg_statements() if is_postgres else _sqlite_statements():
                bind.exec_driver_sql(statement)
    except Exception as error:
        logger.warning(
            "Could not install change tracking on '%s' (%s); an account change there will reach "
            "other processes on their next counter read instead of immediately.",
            _USERS,
            error,
        )


def upgrade() -> None:
    if not _has_table(_USERS):
        op.create_table(
            _USERS,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("username", sa.String(length=120), nullable=False),
            sa.Column("email", sa.String(length=320), nullable=True),
            sa.Column("display_name", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("role", sa.String(length=20), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("password_hash", sa.Text(), nullable=True),
            sa.Column("token_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_by", sa.String(length=36), nullable=True),
            sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("username", name="uq_users_username"),
            sa.UniqueConstraint("email", name="uq_users_email"),
        )
        op.create_index("idx_users_status", _USERS, ["status"])
        op.create_index("idx_users_role", _USERS, ["role"])

    if not _has_table(_TOKENS):
        op.create_table(
            _TOKENS,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "user_id",
                sa.String(length=36),
                sa.ForeignKey(f"{_USERS}.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("purpose", sa.String(length=32), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_by", sa.String(length=36), nullable=True),
            sa.UniqueConstraint("token_hash", name="uq_user_action_tokens_hash"),
        )
        op.create_index("idx_user_action_tokens_user_purpose", _TOKENS, ["user_id", "purpose"])

    _install_change_tracking()


def downgrade() -> None:
    is_postgres = op.get_context().dialect.name == "postgresql"
    drops = (
        [f"DROP TRIGGER IF EXISTS trg_{_USERS}_change ON {_USERS}"]
        if is_postgres
        else [
            f"DROP TRIGGER IF EXISTS trg_{_USERS}_change_{operation}"
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

    if context.is_offline_mode() or _has_table(_TOKENS):
        op.drop_index("idx_user_action_tokens_user_purpose", table_name=_TOKENS)
        op.drop_table(_TOKENS)
    if context.is_offline_mode() or _has_table(_USERS):
        op.drop_index("idx_users_role", table_name=_USERS)
        op.drop_index("idx_users_status", table_name=_USERS)
        op.drop_table(_USERS)
