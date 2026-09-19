"""Immutable ledger of LLM prompt wording, one row per (prompt_key, version).

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-19

``PROMPT_VERSION``-style constants in ``scripts/content_marking.py`` and its
siblings have always been the contract that "sheets from different prompt
versions are not comparable" — but the wording itself only ever lived in git
history. This table makes each distinct wording a queryable, immutable row,
captured automatically at API startup (``PromptRegistryService.sync_from_scripts``)
by running ``scripts/prompt_catalog.py`` and recording any (prompt_key,
version) pair not already seen. Nothing on the scoring hot path reads this
table — the prompt wording in ``scripts/*.py`` stays the sole source of truth
for what actually runs — so, unlike ``revoked_tokens`` or ``notification_reads``,
no change-tracking trigger is installed here.
"""

from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa


revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


_TABLE = "prompt_versions"


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
        sa.Column("prompt_key", sa.String(length=120), nullable=False),
        sa.Column("version", sa.String(length=80), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("template_text", sa.Text(), nullable=False),
        sa.Column("source_script", sa.String(length=120), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("prompt_key", "version", name="uq_prompt_versions_key_version"),
    )
    op.create_index(f"idx_{_TABLE}_key", _TABLE, ["prompt_key"], if_not_exists=True)


def downgrade() -> None:
    if context.is_offline_mode() or _has_table(_TABLE):
        op.drop_table(_TABLE)
