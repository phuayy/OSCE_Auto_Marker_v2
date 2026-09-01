from __future__ import annotations

import asyncio

from app.core.tasks import BackgroundTaskRegistry
from app.database.orm import OrmDatabase
from app.domain.notifications import NotificationType, event_types_match
from app.repositories.notification_repository import NotificationRepository
from app.services.change_feed_service import ChangeFeedService
from app.services.notification_service import NOTIFICATION_EVENT, NotificationService


class _RecordingDispatcher:
    """Captures dispatch calls instead of making HTTP requests."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail
        self.done = asyncio.Event()

    async def dispatch(self, event_type: str, notification: dict) -> int:
        self.calls.append((event_type, notification))
        self.done.set()
        if self.fail:
            raise RuntimeError("endpoint exploded")
        return 1


def _service(tmp_path, *, dispatcher=None, changes=None):
    database = OrmDatabase(tmp_path / "app.sqlite3")
    repository = NotificationRepository(database)
    service = NotificationService(
        repository,
        changes=changes,
        webhooks=dispatcher,
        tasks=BackgroundTaskRegistry(),
    )
    return service, repository


# --- domain filter ---------------------------------------------------------


def test_event_type_matching_rules() -> None:
    assert event_types_match(["scoring.completed"], "scoring.completed") is True
    assert event_types_match(["clips.ready"], "scoring.completed") is False
    assert event_types_match(["*"], "anything.at.all") is True
    # Empty/None means "no filter configured" -> everything.
    assert event_types_match([], "scoring.completed") is True
    assert event_types_match(None, "scoring.completed") is True


# --- emit ------------------------------------------------------------------


def test_emit_persists_the_notification_with_its_type(tmp_path) -> None:
    async def scenario() -> None:
        service, repository = _service(tmp_path)

        stored = await service.emit(
            NotificationType.SCORING_COMPLETED,
            "Scoring complete",
            "Results ready.",
            session_id="session-1",
        )

        assert stored is not None
        assert stored["eventType"] == "scoring.completed"
        assert stored["sessionId"] == "session-1"
        assert stored["read"] is False
        assert stored["id"]
        assert stored["createdAt"]

        rows = await repository.list_rows()
        assert len(rows) == 1
        assert rows[0]["id"] == stored["id"]
        assert await repository.unread_count() == 1

    asyncio.run(scenario())


def test_emit_accepts_a_plain_string_event_type(tmp_path) -> None:
    async def scenario() -> None:
        service, _repository = _service(tmp_path)
        stored = await service.emit("clips.ready", "Clips ready", "3 clips.")
        assert stored["eventType"] == "clips.ready"

    asyncio.run(scenario())


def test_emit_pushes_the_stored_row_to_stream_subscribers(tmp_path) -> None:
    """The browser must learn what happened from the push itself — no re-fetch."""

    async def scenario() -> None:
        changes = ChangeFeedService(OrmDatabase(tmp_path / "app.sqlite3"))
        service, _repository = _service(tmp_path, changes=changes)
        queue = changes.subscribe()

        stored = await service.emit(
            NotificationType.CLIPS_READY, "Clips ready", "Split into 3 clips.", session_id="s-9"
        )

        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert event["type"] == NOTIFICATION_EVENT
        assert event["event"] == "clips.ready"
        assert event["notification"]["id"] == stored["id"]
        assert event["notification"]["title"] == "Clips ready"
        assert event["notification"]["sessionId"] == "s-9"
        assert event["unreadCount"] == 1

    asyncio.run(scenario())


def test_emit_dispatches_to_webhooks(tmp_path) -> None:
    async def scenario() -> None:
        dispatcher = _RecordingDispatcher()
        service, _repository = _service(tmp_path, dispatcher=dispatcher)

        stored = await service.emit(NotificationType.SESSION_FAILED, "Failed", "whisperx died")
        # Dispatch is a background task by design; wait for it to actually run.
        await asyncio.wait_for(dispatcher.done.wait(), timeout=1.0)
        await service.drain()

        assert len(dispatcher.calls) == 1
        event_type, notification = dispatcher.calls[0]
        assert event_type == "session.failed"
        assert notification["id"] == stored["id"]

    asyncio.run(scenario())


def test_webhook_failure_never_breaks_emit(tmp_path) -> None:
    """A dead subscriber endpoint must not fail the pipeline that announced."""

    async def scenario() -> None:
        dispatcher = _RecordingDispatcher(fail=True)
        service, repository = _service(tmp_path, dispatcher=dispatcher)

        stored = await service.emit(NotificationType.SCORING_COMPLETED, "Scoring complete", "Ready.")
        await asyncio.wait_for(dispatcher.done.wait(), timeout=1.0)
        await service.drain()

        # Emit still succeeded and the row is durable.
        assert stored is not None
        assert len(await repository.list_rows()) == 1

    asyncio.run(scenario())


def test_push_failure_never_breaks_emit(tmp_path) -> None:
    class _BrokenChanges:
        def publish_event(self, *_args, **_kwargs):
            raise RuntimeError("bus is down")

    async def scenario() -> None:
        service, repository = _service(tmp_path, changes=_BrokenChanges())

        stored = await service.emit(NotificationType.SCORING_COMPLETED, "Scoring complete", "Ready.")

        assert stored is not None
        assert len(await repository.list_rows()) == 1

    asyncio.run(scenario())


def test_emit_without_collaborators_still_persists(tmp_path) -> None:
    """A bare service (scripts, worker before wiring) degrades to persistence."""

    async def scenario() -> None:
        service = NotificationService(NotificationRepository(OrmDatabase(tmp_path / "app.sqlite3")))
        stored = await service.emit(NotificationType.CLIPS_READY, "Clips ready", "done")
        assert stored is not None
        assert await service.unread_count() == 1

    asyncio.run(scenario())


def test_read_paths_are_delegated(tmp_path) -> None:
    async def scenario() -> None:
        service, _repository = _service(tmp_path)
        stored = await service.emit(NotificationType.SCORING_COMPLETED, "A", "b", session_id="s1")

        assert await service.unread_count() == 1
        assert await service.mark_read(stored["id"]) is True
        assert await service.unread_count() == 0
        assert await service.mark_read("nope") is False

        assert await service.delete_for_session("s1") == 1
        assert await service.list_rows() == []

    asyncio.run(scenario())
