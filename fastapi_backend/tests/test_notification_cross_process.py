"""Notifications raised in one process must reach a browser attached to another.

Under ``JOB_QUEUE_BACKEND=hatchet`` the pipeline runs in the Hatchet worker, so
``NotificationService._publish`` fans out to *that* process's subscriber set —
which is empty, because the browser's SSE stream is held by the API process.
Without a database-level announcement the notification is simply never pushed
and only surfaces on the slow safety poll.

These tests pin the mechanism that closes the gap: the ``notifications`` table is
change-tracked, so an insert bumps a counter (and, on PostgreSQL, fires
pg_notify) that the API process turns into a ``change`` event.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.database.change_tracking import TRACKED_TABLES, install_change_tracking
from app.database.migrations import apply_additive_migrations
from app.database.orm import OrmDatabase
from app.domain.notifications import NotificationType
from app.repositories.notification_repository import NotificationRepository
from app.services.change_feed_service import ChangeFeedService
from app.services.notification_service import NOTIFICATION_EVENT, NotificationService


async def _prepared_database(tmp_path) -> OrmDatabase:
    """A database with the schema and change-tracking triggers installed."""
    database = OrmDatabase(tmp_path / "app.sqlite3")
    await database.initialize()
    await apply_additive_migrations(database.engine)
    await install_change_tracking(database.engine)
    return database


async def _counter(database: OrmDatabase, table: str) -> int:
    async with database.engine.connect() as connection:
        result = await connection.execute(
            text("SELECT version FROM table_versions WHERE table_name = :name"),
            {"name": table},
        )
        row = result.first()
    return int(row[0]) if row else 0


def test_notifications_table_is_change_tracked() -> None:
    """The guard on the whole mechanism. If this table stops being tracked,
    worker-raised notifications go silent again with no other test failing."""
    assert "notifications" in TRACKED_TABLES


def test_inserting_a_notification_bumps_the_change_counter(tmp_path) -> None:
    async def scenario() -> None:
        database = await _prepared_database(tmp_path)
        repository = NotificationRepository(database)

        before = await _counter(database, "notifications")
        await repository.create("Scoring complete", "Results ready.", event_type="scoring.completed")
        after = await _counter(database, "notifications")

        assert after > before, "insert must bump the counter or no process is told"
        await database.shutdown()

    asyncio.run(scenario())


def test_worker_raised_notification_is_visible_to_a_separate_feed(tmp_path) -> None:
    """The actual worker deployment, simulated.

    Two ChangeFeedService instances over one database stand in for the worker and
    API processes. The worker's service has no subscribers; the API-side feed
    must still observe the change through the shared counter.
    """

    async def scenario() -> None:
        database = await _prepared_database(tmp_path)

        # --- "worker process": raises notifications, nobody is listening to it
        worker_feed = ChangeFeedService(database)
        worker_service = NotificationService(NotificationRepository(database), changes=worker_feed)

        # --- "API process": holds the browser's stream
        api_feed = ChangeFeedService(database)
        await api_feed._read_versions_from_db()
        api_queue = api_feed.subscribe()
        baseline = api_feed.versions()["notifications"]

        await worker_service.emit(
            NotificationType.SCORING_COMPLETED, "Scoring complete", "Results ready.", session_id="s-1"
        )

        # The worker's own push went nowhere — that is the bug being guarded.
        assert worker_feed.subscriber_count == 0

        # The API process picks the change up from the shared counter and
        # announces it to the browser.
        await api_feed._read_versions_from_db()
        assert api_feed.versions()["notifications"] > baseline

        api_feed._apply_change("notifications", api_feed.versions()["notifications"])
        event = await asyncio.wait_for(api_queue.get(), timeout=1.0)
        assert event["type"] == "change"
        assert event["table"] == "notifications"

        await database.shutdown()

    asyncio.run(scenario())


def test_watch_loop_announces_a_notification_written_elsewhere(tmp_path) -> None:
    """End to end through the real polling path, no private calls.

    The SQLite fallback watch loop is what a non-PostgreSQL install relies on;
    on PostgreSQL the same announcement arrives faster via LISTEN/NOTIFY.
    """

    async def scenario() -> None:
        database = await _prepared_database(tmp_path)

        api_feed = ChangeFeedService(database, poll_interval_seconds=0.05)
        await api_feed.start(push_enabled=False)
        queue = api_feed.subscribe()
        try:
            # Written by a different service instance, as the worker would.
            other_process = NotificationService(NotificationRepository(database))
            await other_process.emit(NotificationType.CLIPS_READY, "Clips ready", "3 clips.")

            deadline = asyncio.get_running_loop().time() + 3.0
            seen = None
            while asyncio.get_running_loop().time() < deadline:
                event = await asyncio.wait_for(queue.get(), timeout=3.0)
                if event.get("table") == "notifications":
                    seen = event
                    break
            assert seen is not None, "watch loop never announced the notifications change"
            assert seen["type"] == "change"
        finally:
            await api_feed.stop()
            await database.shutdown()

    asyncio.run(scenario())


def test_single_process_still_gets_the_immediate_push(tmp_path) -> None:
    """The counter path is the cross-process fallback, not a replacement.

    In single-process mode the browser must still receive the full notification
    payload immediately, so the toast renders without a refetch.
    """

    async def scenario() -> None:
        database = await _prepared_database(tmp_path)
        feed = ChangeFeedService(database)
        service = NotificationService(NotificationRepository(database), changes=feed)
        queue = feed.subscribe()

        await service.emit(
            NotificationType.SESSION_FAILED, "Processing failed", "whisperx died", session_id="s-2"
        )

        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert event["type"] == NOTIFICATION_EVENT
        assert event["notification"]["title"] == "Processing failed"
        assert event["notification"]["sessionId"] == "s-2"
        assert event["unreadCount"] == 1

        await database.shutdown()

    asyncio.run(scenario())


def test_marking_read_also_announces_a_change(tmp_path) -> None:
    """The unread badge is per-row state; another tab (or process) marking one
    read must not leave this browser's badge stale."""

    async def scenario() -> None:
        database = await _prepared_database(tmp_path)
        repository = NotificationRepository(database)
        stored = await repository.create("Clips ready", "3 clips.", event_type="clips.ready")

        before = await _counter(database, "notifications")
        assert await repository.mark_read(stored["id"]) is True
        after = await _counter(database, "notifications")

        assert after > before
        await database.shutdown()

    asyncio.run(scenario())


def test_marking_all_read_also_announces_a_change(tmp_path) -> None:
    """Dismiss-all is one UPDATE; the counter must still move so every other
    tab's badge (and every other process's cached feed) learns about it."""

    async def scenario() -> None:
        database = await _prepared_database(tmp_path)
        repository = NotificationRepository(database)
        await repository.create("A", "a", event_type="clips.ready")
        await repository.create("B", "b", event_type="clips.ready")

        before = await _counter(database, "notifications")
        assert await repository.mark_all_read() == 2
        after = await _counter(database, "notifications")

        assert after > before
        await database.shutdown()

    asyncio.run(scenario())
