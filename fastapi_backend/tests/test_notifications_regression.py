"""Regression cover for the poll -> push migration.

Every assertion here describes behaviour that existed *before* webhooks and the
push transport were added, and must survive them. They are deliberately written
against the old contracts (the polled REST endpoints, the repository API, the
existing SSE change stream) rather than the new ones, so a future refactor of
the notification stack cannot quietly break clients that still poll.
"""

from __future__ import annotations

import asyncio
import json

from app.database.orm import OrmDatabase
from app.domain.notifications import NotificationType
from app.repositories.notification_repository import NotificationRepository
from app.services.change_feed_service import ChangeFeedService
from app.services.notification_service import NOTIFICATION_EVENT, NotificationService

from tests.test_routes import build_test_client


def _authed(client) -> dict[str, str]:
    response = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


# --- the polled REST API still works (fallback path) -----------------------


def test_notifications_endpoint_keeps_its_response_shape(tmp_path) -> None:
    """The browser still seeds from this on connect, and falls back to polling
    it when the stream is unavailable — so its shape is still load-bearing."""
    client = build_test_client(tmp_path)
    headers = _authed(client)
    container = client.app.state.container

    asyncio.run(
        container.notifications.emit(
            NotificationType.SCORING_COMPLETED,
            "Scoring complete",
            'Scoring is complete for "Session A". Results are ready to review.',
            session_id="session-a",
        )
    )

    body = client.get("/api/notifications", headers=headers).json()
    assert body["unreadCount"] == 1
    assert len(body["notifications"]) == 1
    row = body["notifications"][0]
    # Pre-existing keys must all still be present.
    for key in ("id", "sessionId", "title", "body", "createdAt", "read"):
        assert key in row
    assert row["read"] is False
    assert row["title"] == "Scoring complete"


def test_mark_read_endpoint_still_decrements_the_badge(tmp_path) -> None:
    client = build_test_client(tmp_path)
    headers = _authed(client)
    container = client.app.state.container

    asyncio.run(container.notifications.emit(NotificationType.CLIPS_READY, "Clips ready", "3 clips"))
    notification_id = client.get("/api/notifications", headers=headers).json()["notifications"][0]["id"]

    response = client.post(f"/api/notifications/{notification_id}/read", headers=headers)
    assert response.status_code == 200
    assert response.json()["unreadCount"] == 0

    assert client.post("/api/notifications/does-not-exist/read", headers=headers).status_code == 404


def test_read_all_endpoint_clears_the_badge_in_one_request(tmp_path) -> None:
    """The bell's "dismiss all": one POST, every unread row read, badge zero.
    The response reports what it changed so a client can tell a no-op retry
    from a first call."""
    client = build_test_client(tmp_path)
    headers = _authed(client)
    container = client.app.state.container

    for title in ("Clips ready", "Scoring complete", "Processing failed"):
        asyncio.run(container.notifications.emit(NotificationType.CLIPS_READY, title, "body"))
    assert client.get("/api/notifications", headers=headers).json()["unreadCount"] == 3

    response = client.post("/api/notifications/read-all", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"unreadCount": 0, "markedRead": 3}

    feed = client.get("/api/notifications", headers=headers).json()
    assert feed["unreadCount"] == 0
    assert all(row["read"] is True for row in feed["notifications"])

    # Idempotent, so the browser may resend it after a lost response.
    again = client.post("/api/notifications/read-all", headers=headers)
    assert again.status_code == 200
    assert again.json() == {"unreadCount": 0, "markedRead": 0}

    # The per-row route is untouched by the collection-level one beside it.
    assert client.post("/api/notifications/does-not-exist/read", headers=headers).status_code == 404


def test_notification_endpoints_still_require_auth(tmp_path) -> None:
    client = build_test_client(tmp_path)
    assert client.get("/api/notifications").status_code == 401
    assert client.post("/api/notifications/x/read").status_code == 401
    assert client.post("/api/notifications/read-all").status_code == 401


# --- the repository contract that predates the service ---------------------


def test_repository_notify_still_records_an_unread_row(tmp_path) -> None:
    """`notify(title, body, session_id=)` was the original call shape; callers
    that were never migrated must keep working."""

    async def scenario() -> None:
        repository = NotificationRepository(OrmDatabase(tmp_path / "app.sqlite3"))

        await repository.notify("Scoring complete", "for Session A", session_id="session-a")
        await repository.notify("Clips ready", "3 clips")

        assert await repository.unread_count() == 2
        rows = await repository.list_rows()
        assert len(rows) == 2
        assert rows[0]["title"] == "Clips ready"  # newest first
        assert rows[1]["sessionId"] == "session-a"
        assert all(row["read"] is False for row in rows)

        assert await repository.mark_read(rows[0]["id"]) is True
        assert await repository.mark_read(rows[0]["id"]) is True  # idempotent
        assert await repository.unread_count() == 1
        assert await repository.mark_read("missing") is False

    asyncio.run(scenario())


def test_delete_for_session_still_removes_only_that_session(tmp_path) -> None:
    async def scenario() -> None:
        repository = NotificationRepository(OrmDatabase(tmp_path / "app.sqlite3"))
        await repository.notify("A", "a", session_id="keep")
        await repository.notify("B", "b", session_id="drop")
        await repository.notify("C", "c", session_id="drop")

        assert await repository.delete_for_session("drop") == 2
        remaining = await repository.list_rows()
        assert [row["sessionId"] for row in remaining] == ["keep"]

    asyncio.run(scenario())


# --- the original notification texts ---------------------------------------


def test_completion_and_clip_notification_wording_is_unchanged(tmp_path) -> None:
    """These strings are what the user sees in the feed. The transport changed;
    the message must not have."""

    async def scenario() -> None:
        service = NotificationService(NotificationRepository(OrmDatabase(tmp_path / "app.sqlite3")))

        await service.emit(
            NotificationType.SCORING_COMPLETED,
            "Scoring complete",
            'Scoring is complete for "Demo". Results are ready to review.',
            session_id="s-1",
        )
        await service.emit(
            NotificationType.CLIPS_READY,
            "Clips ready",
            '"Demo" has been split into 3 clips — ready for assessment.',
            session_id="s-1",
        )

        rows = await service.list_rows()
        assert rows[1]["title"] == "Scoring complete"
        assert rows[1]["body"] == 'Scoring is complete for "Demo". Results are ready to review.'
        assert rows[0]["title"] == "Clips ready"
        assert rows[0]["body"] == '"Demo" has been split into 3 clips — ready for assessment.'

    asyncio.run(scenario())


# --- the change stream still carries table changes -------------------------


def test_notification_push_does_not_displace_change_events(tmp_path) -> None:
    """Notifications ride the same bus as table changes. A subscriber must still
    receive both, correctly discriminated by their SSE event name."""

    async def scenario() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        changes = ChangeFeedService(database)
        service = NotificationService(NotificationRepository(database), changes=changes)
        queue = changes.subscribe()

        changes._apply_change("sessions", 7)
        await service.emit(NotificationType.SESSION_FAILED, "Processing failed", "whisperx died")
        changes._apply_change("jobs", 3)

        first = await asyncio.wait_for(queue.get(), timeout=1.0)
        second = await asyncio.wait_for(queue.get(), timeout=1.0)
        third = await asyncio.wait_for(queue.get(), timeout=1.0)

        assert first == {"type": "change", "table": "sessions", "version": 7}
        assert second["type"] == NOTIFICATION_EVENT
        assert second["notification"]["title"] == "Processing failed"
        assert third == {"type": "change", "table": "jobs", "version": 3}

    asyncio.run(scenario())


def test_stream_formats_a_notification_as_its_own_sse_event(tmp_path) -> None:
    """The browser attaches a dedicated 'notification' listener, so the event
    name on the wire is part of the contract."""

    async def scenario() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        changes = ChangeFeedService(database)
        service = NotificationService(NotificationRepository(database), changes=changes)

        stream = changes.stream()
        assert await stream.__anext__() == "retry: 3000\n\n"
        assert (await stream.__anext__()).startswith("event: ready")

        await service.emit(NotificationType.CLIPS_READY, "Clips ready", "2 clips", session_id="s-2")

        frame = await asyncio.wait_for(stream.__anext__(), timeout=1.0)
        assert frame.startswith(f"event: {NOTIFICATION_EVENT}\n")
        payload = json.loads(frame.split("data: ", 1)[1].strip())
        assert payload["event"] == "clips.ready"
        assert payload["notification"]["title"] == "Clips ready"
        assert payload["notification"]["sessionId"] == "s-2"
        await stream.aclose()

    asyncio.run(scenario())


def test_publish_event_refuses_the_reserved_close_name(tmp_path) -> None:
    """'closed' terminates a stream generator; letting an app event use it would
    silently disconnect every subscriber."""
    changes = ChangeFeedService(OrmDatabase(tmp_path / "app.sqlite3"))
    try:
        changes.publish_event("closed", {})
    except ValueError as error:
        assert "reserved" in str(error)
    else:  # pragma: no cover - the guard must fire
        raise AssertionError("publish_event must reject the reserved 'closed' name")


def test_slow_subscriber_drops_oldest_instead_of_blocking_the_emitter(tmp_path) -> None:
    """A browser tab that stops reading must never stall the pipeline that is
    raising notifications."""

    async def scenario() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        changes = ChangeFeedService(database)
        service = NotificationService(NotificationRepository(database), changes=changes)
        queue = changes.subscribe()

        for index in range(400):  # far beyond the per-subscriber bound
            await asyncio.wait_for(
                service.emit(NotificationType.SCORING_COMPLETED, f"n{index}", "body"),
                timeout=2.0,
            )

        assert queue.qsize() <= 256
        # Every notification is still durable, even the ones the slow client lost.
        assert await service.unread_count() == 400

    asyncio.run(scenario())
