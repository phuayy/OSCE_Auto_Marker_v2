from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.database.models import RevokedTokenRecord, utc_now
from app.database.orm import OrmDatabase


class TokenRevocationRepository:
    """Logged-out token ids, durable across restarts and processes.

    ``AuthService`` caches the active set in memory (the same
    ``SnapshotCache`` pattern ``UserDirectory`` uses over ``users``); this
    repository is only ever read from cold or on a cache miss.
    """

    def __init__(self, database: OrmDatabase) -> None:
        self.database = database

    async def revoke(self, token_id: str, user_id: str | None, expires_at: datetime) -> None:
        """Record a revocation. A repeat logout of the same token (a retried
        request, two tabs) is a no-op, not an error — the primary key
        conflict is swallowed rather than raised."""
        values = {
            "token_id": token_id,
            "user_id": user_id,
            "expires_at": expires_at,
        }
        insert = pg_insert if self.database.engine.dialect.name == "postgresql" else sqlite_insert
        stmt = insert(RevokedTokenRecord.__table__).values(**values, revoked_at=utc_now())
        async with self.database.transaction() as db:
            await db.execute(stmt.on_conflict_do_nothing(index_elements=["token_id"]))

    async def list_active_ids(self, now: datetime) -> list[str]:
        """Every revoked token id that has not yet expired — the set a
        replayed token must be checked against. An expired token is refused
        by signature verification anyway, so it is dropped here rather than
        carried forever."""
        async with self.database.session() as db:
            rows = await db.scalars(
                select(RevokedTokenRecord.token_id).where(RevokedTokenRecord.expires_at > now)
            )
            return list(rows)

    async def purge_expired(self, before: datetime) -> int:
        """Delete rows whose token could never be replayed again. Best-effort
        housekeeping, called from ``AuthService.initialize()`` the way
        ``UserRepository.purge_spent_tokens`` is called from
        ``UserAdminService.startup()``."""
        async with self.database.transaction() as db:
            result = await db.execute(delete(RevokedTokenRecord).where(RevokedTokenRecord.expires_at <= before))
        return int(result.rowcount or 0)
