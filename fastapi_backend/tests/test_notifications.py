from __future__ import annotations

import asyncio

from app.database.orm import OrmDatabase
from app.repositories.notification_repository import NotificationRepository


def test_notification_lifecycle(tmp_path) -> None:
    async def _run() -> None:
        repository = NotificationRepository(OrmDatabase(tmp_path / "app.sqlite3"))

        await repository.notify("Scoring complete", 'Scoring is complete for "Session A".', session_id="session-a")
        await repository.notify("Clips ready", '"Session B" has been split into 3 clips.')

        assert await repository.unread_count() == 2
        rows = await repository.list_rows()
        assert len(rows) == 2
        assert rows[0]["title"] == "Clips ready"  # newest first
        assert all(row["read"] is False for row in rows)
        assert rows[1]["sessionId"] == "session-a"

        assert await repository.mark_read(rows[0]["id"]) is True
        assert await repository.unread_count() == 1
        assert await repository.mark_read(rows[0]["id"]) is True  # idempotent
        assert await repository.unread_count() == 1
        assert await repository.mark_read("does-not-exist") is False

        refreshed = await repository.list_rows()
        assert refreshed[0]["read"] is True
        assert refreshed[1]["read"] is False

    asyncio.run(_run())


def test_mark_all_read_touches_only_unread_rows(tmp_path) -> None:
    async def _run() -> None:
        repository = NotificationRepository(OrmDatabase(tmp_path / "app.sqlite3"))
        await repository.notify("A", "a")
        await repository.notify("B", "b")
        await repository.notify("C", "c")
        already_read = (await repository.list_rows())[0]["id"]
        assert await repository.mark_read(already_read) is True

        # Reports what it changed: the two that were still unread, not all three.
        assert await repository.mark_all_read() == 2
        assert await repository.unread_count() == 0
        assert all(row["read"] is True for row in await repository.list_rows())

        # Idempotent: a retry after a lost response has nothing left to mark.
        assert await repository.mark_all_read() == 0
        assert await repository.unread_count() == 0

        # A notification raised afterwards is unread like any other.
        await repository.notify("D", "d")
        assert await repository.unread_count() == 1
        assert (await repository.list_rows())[0]["read"] is False

    asyncio.run(_run())
