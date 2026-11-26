"""Version history for the communication rubric.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-25

Until now the communication rubric existed only as ``auth/communication_rubric.json``
(the document the scorer reads per run) beside the active PDF; replacing it
overwrote both, so an administrator could not go back. This table keeps every
version — upload, reset, saved AI draft, in-place edit, restore — as an
append-only row, one ``active`` and the rest ``retired``; a partial unique index
allows at most one active row. The JSON file stays the scorer's input and is
materialised from the active row. Nothing caches this table, so, like
``prompt_versions``, no change-tracking trigger is installed. Existing
deployments get their current rubric adopted as version 1 at the next startup
(``RubricService.ensure_parsed``), not here: a migration has no business
parsing PDFs.
"""

from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa


revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


_TABLE = "communication_rubric_versions"
_ACTIVE = sa.text("status = 'active'")


def _has_table(name: str) -> bool:
    """A database built by the pre-Alembic ``create_all`` path is stamped at
    0001 and then upgraded, so the ORM may already have created the table."""
    if context.is_offline_mode():
        return False
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("change_kind", sa.String(length=16), nullable=False),
        sa.Column("restored_from_version", sa.Integer(), nullable=True),
        sa.Column(
            "rubric_asset_id",
            sa.String(length=36),
            sa.ForeignKey("rubric_assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("rubric_revision", sa.String(length=64), nullable=False),
        sa.Column("document_json", sa.JSON(), nullable=False),
        sa.Column("created_by_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("version_number", name="uq_communication_rubric_versions_number"),
    )
    op.create_index(
        "uq_communication_rubric_versions_one_active",
        _TABLE,
        ["status"],
        unique=True,
        sqlite_where=_ACTIVE,
        postgresql_where=_ACTIVE,
    )


def downgrade() -> None:
    if context.is_offline_mode() or _has_table(_TABLE):
        op.drop_table(_TABLE)
