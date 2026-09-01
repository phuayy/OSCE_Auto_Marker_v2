"""The database-announcement -> cache-eviction path, end to end.

A trigger cannot write into this process, so the chain is:
``trigger`` -> ``pg_notify`` / counter -> ``ChangeFeedService._apply_change``
-> registered observer -> ``VersionedCache.invalidate_tables``. These tests pin
each link, and then the whole chain through the real container wiring.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.core.config import Settings
from app.core.versioned_cache import VersionedCache
from app.database.change_tracking import install_change_tracking
from app.database.migrations import apply_additive_migrations
from app.database.orm import OrmDatabase
from app.domain.notifications import NotificationType
from app.services.change_feed_service import ChangeFeedService
from app.services.container import create_container


def _settings(tmp_path, **overrides) -> Settings:
    defaults = {
        "root_dir": tmp_path,
        "backend_root": tmp_path,
        "auth_bcrypt_rounds": 4,
        "default_admin_password": "admin",
        "ffmpeg_bin": "ffmpeg",
        "ffprobe_bin": "ffprobe",
        "scorer_python_bin": "python",
        # Force SQLite so tests never reach a real database via the env var.
        "app_database_url": "",
        "database_url": "",
    }
    return Settings(**{**defaults, **overrides})


async def _prepared_database(tmp_path) -> OrmDatabase:
    database = OrmDatabase(tmp_path / "app.sqlite3")
    await database.initialize()
    await apply_additive_migrations(database.engine)
    await install_change_tracking(database.engine)
    return database


# --- the observer hook -----------------------------------------------------


def test_apply_change_notifies_observers(tmp_path) -> None:
    async def scenario() -> None:
        feed = ChangeFeedService(OrmDatabase(tmp_path / "app.sqlite3"))
        seen: list[tuple[str, int]] = []
        feed.add_change_observer(lambda table, version: seen.append((table, version)))

        feed._apply_change("sessions", 5)

        assert seen == [("sessions", 5)]

    asyncio.run(scenario())


def test_observer_runs_before_subscribers_are_told(tmp_path) -> None:
    """Ordering matters: a browser told about a change fetches immediately, and
    must not be served the pre-change value the eviction had not yet removed."""

    async def scenario() -> None:
        feed = ChangeFeedService(OrmDatabase(tmp_path / "app.sqlite3"))
        order: list[str] = []
        feed.add_change_observer(lambda _table, _version: order.append("invalidated"))
        queue = feed.subscribe()

        feed._apply_change("sessions", 1)
        await asyncio.wait_for(queue.get(), timeout=1.0)
        order.append("published")

        assert order == ["invalidated", "published"]

    asyncio.run(scenario())


def test_a_failing_observer_does_not_break_the_stream(tmp_path) -> None:
    """A broken cache hook must never take down the feed that also drives SSE."""

    async def scenario() -> None:
        feed = ChangeFeedService(OrmDatabase(tmp_path / "app.sqlite3"))
        feed.add_change_observer(lambda _t, _v: (_ for _ in ()).throw(RuntimeError("boom")))
        healthy: list[str] = []
        feed.add_change_observer(lambda table, _v: healthy.append(table))
        queue = feed.subscribe()

        feed._apply_change("sessions", 1)

        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert event["table"] == "sessions"
        assert healthy == ["sessions"], "a later observer must still run"

    asyncio.run(scenario())


def test_change_event_evicts_the_matching_cache_entry(tmp_path) -> None:
    async def scenario() -> None:
        feed = ChangeFeedService(OrmDatabase(tmp_path / "app.sqlite3"))
        cache = VersionedCache()
        feed.add_change_observer(lambda table, _v: cache.invalidate_tables((table,)))

        calls = {"count": 0}

        async def build():
            calls["count"] += 1
            return f"build-{calls['count']}"

        await cache.get_or_build("session_index", ("v", 1), build, tables=("sessions",))
        feed._apply_change("sessions", 2)

        # Same token deliberately: the push alone must have evicted it.
        assert await cache.get_or_build("session_index", ("v", 1), build) == "build-2"
        assert calls["count"] == 2

    asyncio.run(scenario())


# --- through the real container --------------------------------------------


def test_container_wires_change_feed_to_the_cache(tmp_path) -> None:
    async def scenario() -> None:
        container = create_container(_settings(tmp_path))
        cache = container.read_cache
        calls = {"count": 0}

        async def build():
            calls["count"] += 1
            return ["projection"]

        await cache.get_or_build("session_index", ("v", 1), build, tables=("sessions",))
        assert cache.stats()["entries"] == 1

        container.changes._apply_change("sessions", 9)
        assert cache.stats()["entries"] == 0, "container must register the cache observer"

    asyncio.run(scenario())


def test_cache_disabled_setting_is_honoured_by_the_container(tmp_path) -> None:
    container = create_container(_settings(tmp_path, cache_enabled=False))
    assert container.read_cache.enabled is False


# --- the notification feed read path ---------------------------------------


def test_notification_feed_is_served_from_cache(tmp_path) -> None:
    """Repeated reads with nothing changed must not re-query the database."""

    async def scenario() -> None:
        container = create_container(_settings(tmp_path))
        # The container's SQLite file lives under the storage layout, which the
        # app creates at startup; these tests bypass startup, so make it here.
        await container.artifacts.ensure_storage_layout()
        await container.orm_database.initialize()
        await apply_additive_migrations(container.orm_database.engine)
        service = container.notifications

        await service.emit(NotificationType.SCORING_COMPLETED, "Scoring complete", "Ready.")

        first = await service.feed()
        misses_after_first = container.read_cache.misses
        second = await service.feed()
        third = await service.feed()

        assert first == second == third
        assert first["unreadCount"] == 1
        assert len(first["notifications"]) == 1
        assert container.read_cache.misses == misses_after_first, (
            "repeat reads must be served from cache"
        )
        assert container.read_cache.hits >= 2

        await container.orm_database.shutdown()

    asyncio.run(scenario())


def test_new_notification_invalidates_the_cached_feed(tmp_path) -> None:
    """A read after a write must never serve the pre-write payload."""

    async def scenario() -> None:
        database = await _prepared_database(tmp_path)
        container = create_container(_settings(tmp_path))
        # Share the prepared database (triggers installed) with the container.
        container.notifications.repository.database = database
        container.changes.database = database

        service = container.notifications
        await service.emit(NotificationType.SCORING_COMPLETED, "First", "one")
        assert len(( await service.feed())["notifications"]) == 1

        await service.emit(NotificationType.CLIPS_READY, "Second", "two")
        refreshed = await service.feed()

        assert len(refreshed["notifications"]) == 2
        assert refreshed["unreadCount"] == 2
        assert refreshed["notifications"][0]["title"] == "Second"

        await database.shutdown()

    asyncio.run(scenario())


def test_marking_read_is_reflected_in_the_next_feed_read(tmp_path) -> None:
    async def scenario() -> None:
        database = await _prepared_database(tmp_path)
        container = create_container(_settings(tmp_path))
        container.notifications.repository.database = database
        container.changes.database = database
        service = container.notifications

        stored = await service.emit(NotificationType.CLIPS_READY, "Clips ready", "3 clips.")
        assert (await service.feed())["unreadCount"] == 1

        assert await service.mark_read(stored["id"]) is True
        after = await service.feed()

        assert after["unreadCount"] == 0
        assert after["notifications"][0]["read"] is True

        await database.shutdown()

    asyncio.run(scenario())


def test_feed_falls_back_to_the_database_without_a_cache(tmp_path) -> None:
    """The cache is an optimisation, never a requirement: a service built
    without one must still answer correctly."""

    async def scenario() -> None:
        from app.repositories.notification_repository import NotificationRepository
        from app.services.notification_service import NotificationService

        database = OrmDatabase(tmp_path / "app.sqlite3")
        await database.initialize()
        await apply_additive_migrations(database.engine)
        service = NotificationService(NotificationRepository(database))

        await service.emit(NotificationType.SESSION_FAILED, "Failed", "whisperx died")
        feed = await service.feed()

        assert feed["unreadCount"] == 1
        assert feed["notifications"][0]["title"] == "Failed"

        await database.shutdown()

    asyncio.run(scenario())


# --- the counter is what makes cross-process invalidation work -------------


def test_a_write_from_another_connection_moves_the_token(tmp_path) -> None:
    """The token is what protects a cache entry when the push announcement never
    arrives — a different process's write, with no listener attached."""

    async def scenario() -> None:
        database = await _prepared_database(tmp_path)
        feed = ChangeFeedService(database)

        before = await feed.token(("notifications",))

        async with database.engine.begin() as connection:
            await connection.exec_driver_sql(
                "INSERT INTO notifications (id, session_id, title, body, created_at) "
                "VALUES ('n-x', 's-1', 'T', 'B', CURRENT_TIMESTAMP)"
            )

        after = await feed.token(("notifications",))
        assert after != before, "the counter must move so cached entries expire"

        async with database.engine.connect() as connection:
            version = (
                await connection.execute(
                    text("SELECT version FROM table_versions WHERE table_name = 'notifications'")
                )
            ).scalar_one()
        assert int(version) >= 1

        await database.shutdown()

    asyncio.run(scenario())
