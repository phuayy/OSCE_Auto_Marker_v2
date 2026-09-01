"""Add notifications.event_type and backfill legacy rows.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-01

Replaces the entry of the same name in ``app/database/migrations.py``
(``ADDITIVE_MIGRATIONS``). That startup pass still runs and is still correct --
it just finds nothing to do once this revision has been applied.

The column is added defensively. A database created by ``Base.metadata
.create_all`` before Alembic existed already has ``event_type``, and the
migration runner stamps such a database at 0001 (not at head) so it still walks
through here. Adding a column that is already present would abort the upgrade,
so the guard is what lets one revision serve both a fresh database and a
pre-Alembic one.
"""

from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


_TABLE = "notifications"
_COLUMN = "event_type"

# Rows written before typed notifications existed were all completions;
# labelling them keeps webhook filtering and the UI consistent.
_BACKFILL = (
    "UPDATE notifications SET event_type = 'scoring.completed' WHERE event_type IS NULL"
)


def _has_column(name: str) -> bool:
    if context.is_offline_mode():
        # ``--sql`` has no database to interrogate; it emits a script for a
        # human to apply. Assume the column is absent, which is the fresh-install
        # case the generated SQL is for.
        return False
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_TABLE):
        return False
    return name in {column["name"] for column in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    if not _has_column(_COLUMN):
        # Nullable on purpose: ``ADD COLUMN NOT NULL`` without a default is
        # rejected by SQLite on a non-empty table, and the backfill below is
        # what gives the existing rows a value.
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(64), nullable=True))

    op.execute(sa.text(_BACKFILL))


def downgrade() -> None:
    if _has_column(_COLUMN):
        op.drop_column(_TABLE, _COLUMN)
