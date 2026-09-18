from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine

from app.database.migration_runner import migrate_engine


async def apply_additive_migrations(engine: AsyncEngine) -> str:
    """Compatibility entry point: adopt pre-Alembic databases and upgrade to head.

    All DDL and backfills belong to Alembic revisions. Empty databases are built
    from scratch; legacy databases are stamped at 0001 before upgrading.
    """
    return await migrate_engine(engine)


__all__ = ["apply_additive_migrations"]
