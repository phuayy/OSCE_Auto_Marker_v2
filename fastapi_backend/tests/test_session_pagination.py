import asyncio

import pytest

from app.core.config import Settings
from app.core.session_cursor import decode_cursor
from app.database.orm import OrmDatabase
from app.repositories.session_repository import SessionRepository
from app.services.session_service import SessionService


def test_pages_have_stable_ties_and_survive_boundary_deletion(tmp_path):
    async def run():
        database = OrmDatabase(tmp_path / "pages.sqlite3")
        repo = SessionRepository(database)
        service = SessionService(Settings(root_dir=tmp_path, backend_root=tmp_path), repo)
        for identity in ["a", "b", "c", "d", "e"]:
            await repo.write({"id": identity, "name": identity, "createdAt": "2026-01-01T00:00:00Z"})
        first = await service.list_page(limit=2)
        assert [row["id"] for row in first["sessions"]] == ["e", "d"]
        await repo.delete("d")
        await repo.write({"id": "new", "name": "new", "createdAt": "2026-02-01T00:00:00Z"})
        second = await service.list_page(limit=2, cursor=first["nextCursor"])
        assert [row["id"] for row in second["sessions"]] == ["c", "b"]
        last = await service.list_page(limit=2, cursor=second["nextCursor"])
        assert [row["id"] for row in last["sessions"]] == ["a"]
        assert last["nextCursor"] is None
        assert len(await service.list_sessions()) == 5
        await database.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("cursor", ["", "???", "e30", "WzEsMl0", "a" * 513])
def test_invalid_cursor_is_rejected(cursor):
    with pytest.raises(ValueError, match="Invalid session cursor"):
        decode_cursor(cursor)
