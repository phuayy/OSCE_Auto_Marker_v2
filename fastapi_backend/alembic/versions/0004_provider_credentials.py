"""Add provider_credentials: encrypted, operator-managed LLM API keys.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-10

The table exists so an API key can be rotated from the settings screen instead
of from the deployment's ``.env`` plus a restart. It deliberately does not live
in ``app_settings``: that table is returned verbatim by ``GET /api/settings``,
so a credential there would be readable by every settings viewer and would sit
in plaintext in every backup.

Only ciphertext is stored. ``ciphertext``/``nonce`` are AES-256-GCM output with
the provider id as additional authenticated data (so a row cannot be moved to
another provider), and ``key_fingerprint`` identifies the master key that sealed
it -- a deployment whose key changed reports "re-enter this key" instead of
decrypting to garbage. ``last4`` is the only part of the key this system ever
shows back, and it is what the settings screen renders.

Created defensively for the same reason as 0002: a database provisioned by the
pre-Alembic ``create_all`` path is stamped at 0001 and walks through every later
revision, and it may already have this table from a newer ``create_all``.
"""

from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


_TABLE = "provider_credentials"


def _has_table() -> bool:
    if context.is_offline_mode():
        # ``--sql`` has no database to interrogate; assume the fresh-install
        # case the generated script is for.
        return False
    return sa.inspect(op.get_bind()).has_table(_TABLE)


def upgrade() -> None:
    if _has_table():
        return
    op.create_table(
        _TABLE,
        sa.Column("provider_id", sa.String(64), primary_key=True),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("nonce", sa.String(64), nullable=False),
        sa.Column("key_fingerprint", sa.String(32), nullable=False, server_default=""),
        sa.Column("last4", sa.String(8), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(120), nullable=True),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_ok", sa.Boolean(), nullable=True),
        sa.Column("last_test_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    if _has_table():
        op.drop_table(_TABLE)
