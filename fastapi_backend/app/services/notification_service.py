from __future__ import annotations

import logging
from typing import Any

from app.core.tasks import BackgroundTaskRegistry
from app.core.versioned_cache import VersionedCache
from app.domain.notifications import NotificationType
from app.repositories.notification_repository import NotificationRepository
from app.services.change_feed_service import ChangeFeedService
from app.services.webhook_dispatcher import WebhookDispatcher


logger = logging.getLogger(__name__)

# SSE event name carrying a notification. Distinct from the change feed's
# "change" events so the browser can render a toast without inspecting a
# discriminator field.
NOTIFICATION_EVENT = "notification"

# Cache key for the combined feed payload served by GET /api/notifications.
NOTIFICATION_FEED_CACHE_KEY = "notification_feed"


class NotificationService:
    """The single place a notification is raised.

    One call fans out to three consumers, in a deliberate order:

    1. **Persist.** The database row is the source of truth — a browser that was
       closed when the event fired still sees it in the feed on next login. If
       this fails there is nothing to announce, so the method stops.
    2. **Push.** The stored row is published on the change feed, which the
       browser is already streaming over ``/api/events``. Reusing that transport
       (rather than opening a second SSE endpoint) means one connection, one
       auth path, and one reconnect story.
    3. **Webhook.** Fanned out to registered HTTP endpoints in a *background*
       task, because a remote endpoint's latency or failure must never propagate
       into the pipeline that raised the event.

    Steps 2 and 3 are best-effort by construction: neither can fail the caller.
    """

    def __init__(
        self,
        repository: NotificationRepository,
        changes: ChangeFeedService | None = None,
        webhooks: WebhookDispatcher | None = None,
        tasks: BackgroundTaskRegistry | None = None,
        cache: VersionedCache | None = None,
    ) -> None:
        self.repository = repository
        # Optional so a bare NotificationService (tests, scripts, the Hatchet
        # worker before wiring) still persists correctly with fan-out disabled.
        self.changes = changes
        self.webhooks = webhooks
        self.tasks = tasks or BackgroundTaskRegistry()
        # Optional too: without a cache (or a change feed to validate it
        # against) every read simply goes to the database, as it always did.
        self.cache = cache

    async def emit(
        self,
        event_type: NotificationType | str,
        title: str,
        body: str,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Raise a notification. Returns the stored row, or None if it was lost.

        Never raises: callers are pipeline steps announcing work they already
        finished, and a failed announcement must not undo that work.
        """
        type_value = event_type.value if isinstance(event_type, NotificationType) else str(event_type)
        notification = await self.repository.create(
            title,
            body,
            session_id=session_id,
            event_type=type_value,
        )
        if notification is None:
            # The repository already logged the cause.
            return None

        await self._publish(type_value, notification)
        self._dispatch_webhooks(type_value, notification)
        return notification

    async def _publish(self, event_type: str, notification: dict[str, Any]) -> None:
        """Push the notification to connected browsers over the change feed."""
        if self.changes is None:
            return
        try:
            unread = await self.repository.unread_count()
        except Exception:
            logger.warning("Could not read unread count for push payload.", exc_info=True)
            unread = None
        try:
            self.changes.publish_event(
                NOTIFICATION_EVENT,
                {
                    "event": event_type,
                    "notification": notification,
                    "unreadCount": unread,
                },
            )
        except Exception:
            logger.exception("Could not push notification %s to subscribers.", notification.get("id"))

    def _dispatch_webhooks(self, event_type: str, notification: dict[str, Any]) -> None:
        """Hand outbound delivery to a tracked background task.

        Tracked rather than bare ``create_task`` because CPython holds only a
        weak reference to a bare task, which can be collected mid-flight —
        exactly the silent-drop failure the registry exists to prevent.
        """
        if self.webhooks is None:
            return
        try:
            self.tasks.spawn(
                self.webhooks.dispatch(event_type, notification),
                name=f"webhook-dispatch:{notification.get('id')}",
            )
        except RuntimeError:
            # No running loop (synchronous context, or shutdown in progress).
            # The notification is already persisted and pushed; only the
            # outbound call is skipped.
            logger.warning("No event loop available for webhook dispatch; skipping outbound delivery.")

    async def drain(self) -> None:
        """Await in-flight webhook deliveries. Called on shutdown."""
        await self.tasks.drain()

    # -- read paths, delegated so routes depend on one collaborator ---------

    async def feed(self, limit: int = 200) -> dict[str, Any]:
        """The payload behind ``GET /api/notifications``, served from cache.

        This endpoint is fetched on every stream reconnect, on every change
        announcement, and on the safety-poll interval, so it is one of the
        hottest reads in the app — and its two queries (the row list and the
        unread count) always move together. Caching them as one entry means a
        browser reconnect storm costs a single pair of queries rather than a
        pair per client.

        The entry is evicted by a write to ``notifications``, and its token is
        that table's change counter, so a hit can never be stale.
        """
        if self.cache is None or self.changes is None:
            return await self._build_feed(limit)

        token = await self.changes.token(("notifications",))
        return await self.cache.get_or_build(
            f"{NOTIFICATION_FEED_CACHE_KEY}:{limit}",
            token,
            lambda: self._build_feed(limit),
            tables=("notifications",),
        )

    async def _build_feed(self, limit: int) -> dict[str, Any]:
        return {
            "notifications": await self.repository.list_rows(limit),
            "unreadCount": await self.repository.unread_count(),
        }

    async def list_rows(self, limit: int = 200) -> list[dict[str, Any]]:
        return await self.repository.list_rows(limit)

    async def unread_count(self) -> int:
        return await self.repository.unread_count()

    async def mark_read(self, notification_id: str) -> bool:
        return await self.repository.mark_read(notification_id)

    async def delete_for_session(self, session_id: str) -> int:
        return await self.repository.delete_for_session(session_id)

    async def notify(self, title: str, body: str, *, session_id: str | None = None) -> None:
        """Backwards-compatible shim for the pre-typed call shape.

        Retained so any caller not yet migrated keeps working; new code should
        call :meth:`emit` with an explicit :class:`NotificationType`.
        """
        await self.emit(NotificationType.SCORING_COMPLETED, title, body, session_id=session_id)
