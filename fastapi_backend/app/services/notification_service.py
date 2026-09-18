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

        self._evict_feed()
        await self._publish(type_value, notification)
        self._dispatch_webhooks(type_value, notification)
        return notification

    async def _publish(self, event_type: str, notification: dict[str, Any]) -> None:
        """Push the notification to connected browsers over the change feed.

        No ``unreadCount`` in this payload: read state is per-viewer (see
        :meth:`feed`) and this is one broadcast to every connected browser, so
        there is no single count that would be correct for all of them. The
        browser refetches its own count from ``GET /api/notifications``
        instead — the same thing it already does for a cross-process
        ``notifications`` change event.
        """
        if self.changes is None:
            return
        try:
            self.changes.publish_event(
                NOTIFICATION_EVENT,
                {
                    "event": event_type,
                    "notification": notification,
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

    async def feed(self, limit: int = 200, *, viewer_id: str) -> dict[str, Any]:
        """The payload behind ``GET /api/notifications``, served from cache.

        This endpoint is fetched on every stream reconnect, on every change
        announcement, and on the safety-poll interval, so it is one of the
        hottest reads in the app — and its two queries (the row list and the
        unread count) always move together. Caching them as one entry means a
        browser reconnect storm costs a single pair of queries rather than a
        pair per client. The entry is keyed on ``viewer_id`` because read
        state is per-viewer (see ``app/repositories/notification_repository.py``);
        two markers open at once must never share a cached feed.

        The entry is evicted by a write to ``notifications`` *or*
        ``notification_reads``, and its token is those tables' combined change
        counter, so a hit can never be stale. This process's own writes evict
        it directly as well (:meth:`_evict_feed`), so the response to a
        mark-read is never built from the feed it just changed.
        """
        if self.cache is None or self.changes is None:
            return await self._build_feed(limit, viewer_id=viewer_id)

        token = await self.changes.token(("notifications", "notification_reads"))
        return await self.cache.get_or_build(
            f"{NOTIFICATION_FEED_CACHE_KEY}:{viewer_id}:{limit}",
            token,
            lambda: self._build_feed(limit, viewer_id=viewer_id),
            tables=("notifications", "notification_reads"),
        )

    async def _build_feed(self, limit: int, *, viewer_id: str) -> dict[str, Any]:
        return {
            "notifications": await self.repository.list_rows(limit, viewer_id=viewer_id),
            "unreadCount": await self.repository.unread_count(viewer_id=viewer_id),
        }

    def _evict_feed(self) -> None:
        """Drop every viewer's cached feed after a write made by this process.

        The database announces every committed write and the change feed evicts
        on that announcement, but with the PostgreSQL listener connected the
        cache token is answered from memory, so between the commit and the
        announcement's arrival a read in *this* process would still hit the
        pre-write entry — and the browser that just marked everything read
        polls, reconnects and refetches on exactly that kind of boundary. The
        announcement still reaches every other process; this only closes the
        read-your-writes window in the one that wrote. Every viewer's entry is
        dropped (not just the writer's) because ``invalidate_tables`` matches
        by table, not by cache key — cheap, since this table is written a
        handful of times per session, not per request.
        """
        if self.cache is None:
            return
        self.cache.invalidate_tables(("notifications", "notification_reads"))

    async def list_rows(self, limit: int = 200, *, viewer_id: str) -> list[dict[str, Any]]:
        return await self.repository.list_rows(limit, viewer_id=viewer_id)

    async def unread_count(self, *, viewer_id: str) -> int:
        return await self.repository.unread_count(viewer_id=viewer_id)

    async def mark_read(self, notification_id: str, *, viewer_id: str) -> bool:
        marked = await self.repository.mark_read(notification_id, viewer_id=viewer_id)
        if marked:
            self._evict_feed()
        return marked

    async def mark_all_read(self, *, viewer_id: str) -> int:
        """Mark every notification this viewer has not read; returns how many
        were unread.

        One repository call, one transaction, one change announcement — the
        "dismiss all" control must not be a loop over :meth:`mark_read`, which
        would announce and refetch once per row. Scoped to ``viewer_id``: this
        must never touch another marker's read state.
        """
        marked = await self.repository.mark_all_read(viewer_id=viewer_id)
        if marked:
            self._evict_feed()
        return marked

    async def delete_for_session(self, session_id: str) -> int:
        deleted = await self.repository.delete_for_session(session_id)
        if deleted:
            self._evict_feed()
        return deleted

    async def notify(self, title: str, body: str, *, session_id: str | None = None) -> None:
        """Backwards-compatible shim for the pre-typed call shape.

        Retained so any caller not yet migrated keeps working; new code should
        call :meth:`emit` with an explicit :class:`NotificationType`.
        """
        await self.emit(NotificationType.SCORING_COMPLETED, title, body, session_id=session_id)
