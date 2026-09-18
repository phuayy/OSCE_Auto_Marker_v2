from __future__ import annotations

import asyncio

from app.database.orm import OrmDatabase
from app.domain.users import UserRole, UserStatus
from app.repositories.notification_repository import NotificationRepository
from app.repositories.user_repository import UserRepository


# notification_reads.user_id is a real foreign key to users.id (ON DELETE
# CASCADE), the same reason test_user_settings.py gives for creating real
# account rows rather than referencing a bare string id.


async def _account(users: UserRepository, username: str) -> str:
    record = await users.create(
        username=username,
        email=f"{username}@example.edu",
        display_name="",
        role=UserRole.MARKER,
        status=UserStatus.ACTIVE,
        password_hash=None,
    )
    return record.id


def test_notification_lifecycle(tmp_path) -> None:
    async def _run() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        repository = NotificationRepository(database)
        viewer = await _account(UserRepository(database), "viewer")

        await repository.notify("Scoring complete", 'Scoring is complete for "Session A".', session_id="session-a")
        await repository.notify("Clips ready", '"Session B" has been split into 3 clips.')

        assert await repository.unread_count(viewer_id=viewer) == 2
        rows = await repository.list_rows(viewer_id=viewer)
        assert len(rows) == 2
        assert rows[0]["title"] == "Clips ready"  # newest first
        assert all(row["read"] is False for row in rows)
        assert rows[1]["sessionId"] == "session-a"

        assert await repository.mark_read(rows[0]["id"], viewer_id=viewer) is True
        assert await repository.unread_count(viewer_id=viewer) == 1
        assert await repository.mark_read(rows[0]["id"], viewer_id=viewer) is True  # idempotent
        assert await repository.unread_count(viewer_id=viewer) == 1
        assert await repository.mark_read("does-not-exist", viewer_id=viewer) is False

        refreshed = await repository.list_rows(viewer_id=viewer)
        assert refreshed[0]["read"] is True
        assert refreshed[1]["read"] is False

    asyncio.run(_run())


def test_read_state_is_per_viewer(tmp_path) -> None:
    """The bug this table exists to fix: one marker's read state must never
    be visible as another marker's."""

    async def _run() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        repository = NotificationRepository(database)
        users = UserRepository(database)
        first = await _account(users, "first")
        second = await _account(users, "second")

        await repository.notify("Scoring complete", "Ready.")
        notification_id = (await repository.list_rows(viewer_id=first))[0]["id"]

        assert await repository.mark_read(notification_id, viewer_id=first) is True

        assert await repository.unread_count(viewer_id=first) == 0
        assert await repository.unread_count(viewer_id=second) == 1
        assert (await repository.list_rows(viewer_id=first))[0]["read"] is True
        assert (await repository.list_rows(viewer_id=second))[0]["read"] is False

    asyncio.run(_run())


def test_mark_all_read_touches_only_this_viewers_unread_rows(tmp_path) -> None:
    async def _run() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        repository = NotificationRepository(database)
        users = UserRepository(database)
        first = await _account(users, "first")
        second = await _account(users, "second")

        await repository.notify("A", "a")
        await repository.notify("B", "b")
        await repository.notify("C", "c")
        already_read = (await repository.list_rows(viewer_id=first))[0]["id"]
        assert await repository.mark_read(already_read, viewer_id=first) is True

        # Reports what it changed: the two that were still unread for this
        # viewer, not all three.
        assert await repository.mark_all_read(viewer_id=first) == 2
        assert await repository.unread_count(viewer_id=first) == 0
        assert all(row["read"] is True for row in await repository.list_rows(viewer_id=first))

        # A second viewer who never marked anything read is untouched.
        assert await repository.unread_count(viewer_id=second) == 3
        assert all(row["read"] is False for row in await repository.list_rows(viewer_id=second))

        # Idempotent: a retry after a lost response has nothing left to mark.
        assert await repository.mark_all_read(viewer_id=first) == 0
        assert await repository.unread_count(viewer_id=first) == 0

        # A notification raised afterwards is unread like any other, for
        # every viewer.
        await repository.notify("D", "d")
        assert await repository.unread_count(viewer_id=first) == 1
        assert (await repository.list_rows(viewer_id=first))[0]["read"] is False

    asyncio.run(_run())
