from __future__ import annotations

import asyncio

from app.core.config import Settings
from app.core.versioned_cache import VersionedCache
from app.database.change_tracking import install_change_tracking
from app.database.orm import OrmDatabase
from app.repositories.session_repository import SessionRepository
from app.services.change_feed_service import ChangeFeedService
from app.services.session_service import SessionService


def _session(session_id: str, name: str, **overrides) -> dict:
    payload = {
        "id": session_id,
        "name": name,
        "status": "uploaded",
        "createdAt": "2026-01-01T00:00:00Z",
        "outputs": {},
    }
    payload.update(overrides)
    return payload


async def _build(tmp_path) -> tuple[OrmDatabase, ChangeFeedService, VersionedCache, SessionService, SessionRepository]:
    database = OrmDatabase(tmp_path / "app.sqlite3")
    await database.initialize()
    await install_change_tracking(database.engine)
    changes = ChangeFeedService(database, poll_interval_seconds=0.05)
    await changes.start(push_enabled=False)
    cache = VersionedCache()
    repository = SessionRepository(database)
    service = SessionService(Settings.load(), repository, cache=cache, changes=changes)
    return database, changes, cache, service, repository


def test_triggers_bump_version_on_insert_update_and_delete(tmp_path) -> None:
    async def _run() -> None:
        database, changes, _cache, _service, repository = await _build(tmp_path)
        try:
            assert changes.versions()["sessions"] == 0

            await repository.write(_session("s1", "Alpha"))
            after_insert = await changes.token(("sessions",))

            await repository.write(_session("s1", "Alpha", status="processing"))
            after_update = await changes.token(("sessions",))
            assert after_update != after_insert

            await repository.delete("s1")
            after_delete = await changes.token(("sessions",))
            assert after_delete != after_update
        finally:
            await changes.stop()
            await database.shutdown()

    asyncio.run(_run())


def test_session_index_is_served_from_cache_until_a_write(tmp_path) -> None:
    async def _run() -> None:
        database, changes, cache, service, repository = await _build(tmp_path)
        try:
            await repository.write(_session("s1", "Alpha"))

            first = await service.list_sessions()
            second = await service.list_sessions()
            assert first == second
            # Second call rebuilt nothing.
            assert cache.stats()["misses"] == 1
            assert cache.stats()["hits"] == 1

            # A write must invalidate, even though nothing told the cache directly.
            await repository.write(_session("s2", "Beta", createdAt="2026-01-02T00:00:00Z"))
            third = await service.list_sessions()
            assert len(third) == 2
            assert cache.stats()["misses"] == 2
        finally:
            await changes.stop()
            await database.shutdown()

    asyncio.run(_run())


def test_index_projection_reports_pipeline_and_clip_fields(tmp_path) -> None:
    async def _run() -> None:
        database, changes, _cache, service, repository = await _build(tmp_path)
        try:
            await repository.write(
                _session(
                    "s1",
                    "Alpha",
                    workflow="long",
                    segmentation="person",
                    pipeline={"currentStep": "whisperx", "startedAt": "2026-01-01T00:00:05Z"},
                    outputs={"videoClips": [{"id": "c1", "label": "Student 1"}]},
                )
            )
            await repository.write(_session("s2", "Beta", createdAt="2026-01-02T00:00:00Z"))

            rows = {row["id"]: row for row in await service.list_sessions()}

            assert rows["s1"]["workflow"] == "long"
            assert rows["s1"]["segmentation"] == "person"
            assert rows["s1"]["currentStep"] == "whisperx"
            assert rows["s1"]["pipelineStartedAt"] == "2026-01-01T00:00:05Z"
            assert rows["s1"]["hasVideoClips"] is True
            # A session with no clips must report False, not None.
            assert rows["s2"]["hasVideoClips"] is False
            assert rows["s2"]["currentStep"] is None
        finally:
            await changes.stop()
            await database.shutdown()

    asyncio.run(_run())


def test_index_projection_matches_legacy_full_read_shape(tmp_path) -> None:
    """The cached projection must expose exactly the keys the frontend cards read."""

    async def _run() -> None:
        database, changes, _cache, service, repository = await _build(tmp_path)
        try:
            await repository.write(
                _session("s1", "Alpha", workflow="standard", parentSessionId=None)
            )
            [row] = await service.list_sessions()
            assert set(row) == {
                "id",
                "name",
                "createdAt",
                "status",
                "workflow",
                "segmentation",
                "parentSessionId",
                "clipSource",
                "hasVideoClips",
                "currentStep",
                "stepProgress",
                "pipelineStartedAt",
                "clipExportStatus",
                "clipExportCompleted",
                "clipExportTotal",
            }
        finally:
            await changes.stop()
            await database.shutdown()

    asyncio.run(_run())


def test_unnamed_sessions_are_backfilled_then_cached(tmp_path) -> None:
    async def _run() -> None:
        database, changes, _cache, service, repository = await _build(tmp_path)
        try:
            await repository.write(_session("s1", ""))
            await repository.write(_session("s2", "", createdAt="2026-01-02T00:00:00Z"))

            rows = await service.list_sessions()
            names = [row["name"] for row in rows]
            assert all(name for name in names), "every session should end up named"
            assert len(set(names)) == 2, "backfilled names must be unique"
        finally:
            await changes.stop()
            await database.shutdown()

    asyncio.run(_run())


def test_watch_loop_publishes_change_events_to_subscribers(tmp_path) -> None:
    async def _run() -> None:
        database, changes, _cache, _service, repository = await _build(tmp_path)
        queue = changes.subscribe()
        try:
            await repository.write(_session("s1", "Alpha"))
            event = await asyncio.wait_for(queue.get(), timeout=5.0)
            assert event["type"] == "change"
            assert event["table"] == "sessions"
        finally:
            changes.unsubscribe(queue)
            await changes.stop()
            await database.shutdown()

    asyncio.run(_run())


def test_service_without_cache_still_lists_sessions(tmp_path) -> None:
    """A bare SessionService (scripts, older tests) must keep working."""

    async def _run() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        await database.initialize()
        repository = SessionRepository(database)
        service = SessionService(Settings.load(), repository)
        try:
            await repository.write(_session("s1", "Alpha"))
            rows = await service.list_sessions()
            assert [row["id"] for row in rows] == ["s1"]
        finally:
            await database.shutdown()

    asyncio.run(_run())
