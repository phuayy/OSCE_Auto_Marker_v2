from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.database.orm import OrmDatabase
from app.repositories.token_revocation_repository import TokenRevocationRepository


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_revoke_and_list_active_ids(tmp_path) -> None:
    async def scenario() -> None:
        repository = TokenRevocationRepository(OrmDatabase(tmp_path / "app.sqlite3"))
        now = _now()

        await repository.revoke("t-1", "user-1", now + timedelta(hours=8))
        await repository.revoke("t-2", None, now + timedelta(hours=8))

        active = await repository.list_active_ids(now)
        assert set(active) == {"t-1", "t-2"}

    asyncio.run(scenario())


def test_revoke_is_idempotent(tmp_path) -> None:
    """A repeated logout of the same token (a retried request, two tabs) must
    not raise on the primary key."""

    async def scenario() -> None:
        repository = TokenRevocationRepository(OrmDatabase(tmp_path / "app.sqlite3"))
        now = _now()

        await repository.revoke("t-1", "user-1", now + timedelta(hours=8))
        await repository.revoke("t-1", "user-1", now + timedelta(hours=8))  # no-op, not an error

        assert await repository.list_active_ids(now) == ["t-1"]

    asyncio.run(scenario())


def test_list_active_ids_excludes_expired_rows(tmp_path) -> None:
    """A token past its own expiry is refused by signature verification
    regardless; it must not linger in the active set forever."""

    async def scenario() -> None:
        repository = TokenRevocationRepository(OrmDatabase(tmp_path / "app.sqlite3"))
        now = _now()

        await repository.revoke("expired", "user-1", now - timedelta(minutes=1))
        await repository.revoke("still-active", "user-1", now + timedelta(hours=1))

        assert await repository.list_active_ids(now) == ["still-active"]

    asyncio.run(scenario())


def test_purge_expired_removes_only_expired_rows(tmp_path) -> None:
    async def scenario() -> None:
        repository = TokenRevocationRepository(OrmDatabase(tmp_path / "app.sqlite3"))
        now = _now()

        await repository.revoke("expired-1", "user-1", now - timedelta(days=1))
        await repository.revoke("expired-2", "user-1", now - timedelta(minutes=1))
        await repository.revoke("still-active", "user-1", now + timedelta(hours=1))

        removed = await repository.purge_expired(now)

        assert removed == 2
        assert await repository.list_active_ids(now) == ["still-active"]

    asyncio.run(scenario())
