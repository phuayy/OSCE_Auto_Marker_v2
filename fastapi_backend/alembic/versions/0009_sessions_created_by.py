"""Record which account created each session.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-17

Accounts exist now (``0008``), so a session can say who uploaded it and a clip
child who queued its assessment. The whole identity snapshot — user id,
username, display name as they were at that moment — lives in the payload
under ``createdBy``; this column mirrors the user id out of it, the way
``parent_session_id`` mirrors the parent, so "sessions by this account" is an
indexed equality rather than a JSON scan.

No backfill: a session recorded before this revision genuinely has no creator,
and inventing one would be a lie the audit trail then repeats. Mirrors the
``sessions.created_by`` entry in ``ADDITIVE_MIGRATIONS``; that startup pass
adds the column (not the index) when Alembic is not the one migrating.
"""

from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa


revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


_TABLE = "sessions"
_COLUMN = "created_by"
_INDEX = "idx_sessions_created_by"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_column() -> bool:
    if context.is_offline_mode():
        return False
    inspector = _inspector()
    if not inspector.has_table(_TABLE):
        return False
    return _COLUMN in {column["name"] for column in inspector.get_columns(_TABLE)}


def _has_index() -> bool:
    if context.is_offline_mode():
        return False
    inspector = _inspector()
    if not inspector.has_table(_TABLE):
        return False
    return _INDEX in {index["name"] for index in inspector.get_indexes(_TABLE)}


def upgrade() -> None:
    # Guarded like 0002: a pre-Alembic database that already ran the additive
    # fallback has the column; adding it again would abort the upgrade.
    if not _has_column():
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=36), nullable=True))
    if not _has_index():
        op.create_index(_INDEX, _TABLE, [_COLUMN])


def downgrade() -> None:
    if context.is_offline_mode() or _has_index():
        op.drop_index(_INDEX, table_name=_TABLE)
    if context.is_offline_mode() or _has_column():
        with op.batch_alter_table(_TABLE, schema=None) as batch_op:
            batch_op.drop_column(_COLUMN)
