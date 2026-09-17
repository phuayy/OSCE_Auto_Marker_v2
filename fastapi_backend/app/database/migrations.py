from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AddColumn:
    """One additive column migration.

    ``Base.metadata.create_all`` creates missing *tables* but never alters an
    existing one, so a column added to a model after a database already exists
    is silently absent until something SELECTs it and the query fails. This
    describes the ALTER needed to close that gap.

    ``definition`` is the column's SQL type plus any DEFAULT — deliberately
    written to be valid on both SQLite and PostgreSQL, since the app runs on
    either. Only nullable / defaulted columns belong here: ``ADD COLUMN NOT NULL``
    without a default is rejected by SQLite on a non-empty table.
    """

    table: str
    column: str
    definition: str
    # Optional one-shot backfill for rows that predate the column.
    backfill_sql: str | None = None


# Applied in order at startup. Append only — never edit or remove an entry, or
# a database that already ran it will diverge from one that has not.
ADDITIVE_MIGRATIONS: tuple[AddColumn, ...] = (
    AddColumn(
        table="notifications",
        column="event_type",
        definition="VARCHAR(64)",
        # Rows written before typed notifications existed were all completions;
        # labelling them keeps webhook filtering and the UI consistent.
        backfill_sql="UPDATE notifications SET event_type = 'scoring.completed' WHERE event_type IS NULL",
    ),
    # Who created the session (revision 0009). No backfill: rows from before
    # creators were recorded genuinely have none. The index that revision adds
    # is not reproduced here — this fallback keeps queries *working*, not fast.
    AddColumn(table="sessions", column="created_by", definition="VARCHAR(36)"),
)


async def _existing_columns(engine: AsyncEngine, table: str) -> set[str]:
    def _read(connection) -> set[str]:
        inspector = inspect(connection)
        if not inspector.has_table(table):
            # The table itself is missing, which means create_all has not run or
            # this deployment does not use it. Either way there is nothing to
            # alter; report "no columns" and let the caller skip.
            return set()
        return {column["name"] for column in inspector.get_columns(table)}

    async with engine.connect() as connection:
        return await connection.run_sync(_read)


async def apply_additive_migrations(engine: AsyncEngine) -> list[str]:
    """Add any model column missing from an existing database.

    Runs after ``create_all`` on every boot and is idempotent: a column that is
    already present is skipped without a write. Returns the identifiers applied,
    for logging and tests.

    A failure here is *not* swallowed. Unlike the change-tracking triggers —
    where the fallback is merely slower — a missing column makes every query
    touching it fail at request time. Failing the boot surfaces that at the one
    moment an operator is watching.
    """
    applied: list[str] = []

    for migration in ADDITIVE_MIGRATIONS:
        columns = await _existing_columns(engine, migration.table)
        if not columns or migration.column in columns:
            continue

        identifier = f"{migration.table}.{migration.column}"
        statement = (
            f"ALTER TABLE {migration.table} ADD COLUMN {migration.column} {migration.definition}"
        )
        async with engine.begin() as connection:
            await connection.exec_driver_sql(statement)
            if migration.backfill_sql:
                await connection.exec_driver_sql(migration.backfill_sql)
        applied.append(identifier)
        logger.info("Applied additive migration: added column %s.", identifier)

    return applied


__all__ = ["ADDITIVE_MIGRATIONS", "AddColumn", "apply_additive_migrations"]
