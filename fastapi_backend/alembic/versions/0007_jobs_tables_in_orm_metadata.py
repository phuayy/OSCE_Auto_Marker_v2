"""The jobs tables join the ORM metadata, and their primary keys become NOT NULL.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-13

The queue's three tables used to be described by hand, as raw ``CREATE TABLE``
strings in ``app/database/schema.py``, and reached through a second connection
pool of their own. One database with two schema sources meant autogenerate had
to be told to ignore half of it and ``alembic check`` was blind there. That
layer is gone: ``jobs``, ``job_attempts`` and ``job_events`` are ordinary models
in ``app.database.models`` now, and this migration is what the first ``alembic
check`` over them found.

The drift is real, not cosmetic. SQLite only implies NOT NULL for an
``INTEGER PRIMARY KEY``; for any other primary key — ``jobs.id`` is ``TEXT`` —
it permits a NULL, and the hand-written DDL never said otherwise. A job row with
a null id would be unreadable and unclaimable. PostgreSQL has always made
primary keys NOT NULL, so there the statements below are a no-op.

SQLite cannot alter a column in place, so batch mode rebuilds each table. The
rebuild drops the ``AUTOINCREMENT`` keyword from the two surrogate keys, leaving
them plain ``INTEGER PRIMARY KEY`` (rowid aliases). That is deliberate and safe:
the ids are read only to order the attempts and events *of one job*, and those
rows are deleted only together with that job, so a reused rowid can never
reorder a job's own history.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.alter_column("id", existing_type=sa.TEXT(), nullable=False)

    with op.batch_alter_table("job_attempts", schema=None) as batch_op:
        batch_op.alter_column("id", existing_type=sa.INTEGER(), nullable=False, autoincrement=True)

    with op.batch_alter_table("job_events", schema=None) as batch_op:
        batch_op.alter_column("id", existing_type=sa.INTEGER(), nullable=False, autoincrement=True)


def downgrade() -> None:
    with op.batch_alter_table("job_events", schema=None) as batch_op:
        batch_op.alter_column("id", existing_type=sa.INTEGER(), nullable=True, autoincrement=True)

    with op.batch_alter_table("job_attempts", schema=None) as batch_op:
        batch_op.alter_column("id", existing_type=sa.INTEGER(), nullable=True, autoincrement=True)

    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.alter_column("id", existing_type=sa.TEXT(), nullable=True)
