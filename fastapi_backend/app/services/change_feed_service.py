from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Callable, Hashable

from sqlalchemy import select

from app.database.change_tracking import CHANGE_CHANNEL, TRACKED_TABLES
from app.database.models import TableVersionRecord
from app.database.orm import OrmDatabase


logger = logging.getLogger(__name__)

# Reconnect backoff bounds for the Postgres listener.
_RECONNECT_MIN_SECONDS = 1.0
_RECONNECT_MAX_SECONDS = 30.0

# Per-subscriber queue bound. A stalled SSE client drops its oldest events
# rather than blocking the listener that feeds every other subscriber.
_SUBSCRIBER_QUEUE_MAXSIZE = 256


class ChangeFeedService:
    """Tracks which tables have changed, and pushes those changes to subscribers.

    Two responsibilities, both built on the trigger-maintained ``table_versions``
    counters:

    * **Cache tokens** — :meth:`token` returns a cheap, comparable snapshot of
      every tracked table's version, which :class:`VersionedCache` uses to decide
      whether a cached projection is still valid.
    * **Push** — on PostgreSQL a dedicated connection holds ``LISTEN`` on the
      change channel, so a write by *any* process (notably the Hatchet worker)
      reaches this process within milliseconds. Subscribers, in turn, feed the
      browser's SSE stream, which is what lets the frontend stop polling.

    On SQLite there is no NOTIFY, so :meth:`token` reads the counters directly
    and subscribers are driven by an internal watch loop. Behaviour is identical;
    only latency and the per-request cost differ.
    """

    def __init__(self, database: OrmDatabase, *, poll_interval_seconds: float = 2.0) -> None:
        self.database = database
        self.poll_interval_seconds = poll_interval_seconds
        self._versions: dict[str, int] = {table: 0 for table in TRACKED_TABLES}
        self._push_active = False
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        # In-process reactions to a committed write (see add_change_observer).
        # The application cache registers here so a trigger's announcement
        # evicts stale entries without any polling query.
        self._change_observers: list[Callable[[str, int], None]] = []
        self._read_lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    async def start(self, *, push_enabled: bool) -> None:
        """Begin tracking. ``push_enabled`` comes from the trigger installer and
        is True only when the Postgres LISTEN/NOTIFY path is available."""
        self._stopping.clear()
        await self._read_versions_from_db()
        if push_enabled and self._dsn():
            self._task = asyncio.create_task(self._listen_loop(), name="change-feed-listen")
        else:
            self._task = asyncio.create_task(self._watch_loop(), name="change-feed-watch")

    async def stop(self) -> None:
        self._stopping.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutdown must not raise
                pass
        for queue in list(self._subscribers):
            self._offer(queue, {"type": "closed"})
        self._subscribers.clear()

    # -- cache tokens ------------------------------------------------------

    async def token(self, tables: tuple[str, ...] = TRACKED_TABLES) -> Hashable:
        """A comparable snapshot of ``tables``' versions, for cache validation.

        With the listener connected this is answered from memory, so a cache hit
        costs no database round-trip at all. Without it, the counters are read
        fresh on every call — deliberately *not* memoised: a time-based memo
        would let a cache entry outlive the write that invalidated it, and this
        read is a single indexed lookup of a handful of rows, far cheaper than
        the projection it guards.
        """
        if not self._push_active:
            async with self._read_lock:
                await self._read_versions_from_db()
        return tuple((table, self._versions.get(table, 0)) for table in tables)

    def versions(self) -> dict[str, int]:
        return dict(self._versions)

    @property
    def push_active(self) -> bool:
        """True when changes are pushed by the database rather than polled for."""
        return self._push_active

    async def _read_versions_from_db(self) -> None:
        try:
            async with self.database.session() as db:
                rows = (
                    await db.execute(
                        select(TableVersionRecord.table_name, TableVersionRecord.version)
                    )
                ).all()
            for table_name, version in rows:
                self._versions[str(table_name)] = int(version or 0)
        except Exception:
            # A read failure must not break the request path — callers fall back
            # to the last known versions, which at worst forces a cache rebuild.
            logger.warning("Could not read table versions; using last known snapshot.", exc_info=True)

    # -- subscribers -------------------------------------------------------

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAXSIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish_event(self, event_name: str, payload: dict[str, Any]) -> None:
        """Put an application event on the same bus the table changes ride.

        The change feed's transport — bounded per-subscriber queues, drop-oldest
        under back-pressure, one authenticated SSE connection per browser — is
        exactly what any server-to-client push needs, so notifications reuse it
        rather than standing up a second stream with its own auth and reconnect
        behaviour. ``event_name`` becomes the SSE ``event:`` line, letting the
        browser attach a dedicated listener.

        Synchronous and non-blocking: safe to call from anywhere in the event
        loop, and a stalled subscriber can never delay the caller.
        """
        if event_name == "closed":
            raise ValueError("'closed' is reserved for stream shutdown.")
        self._publish({**payload, "type": event_name})

    def _publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            self._offer(queue, event)

    @staticmethod
    def _offer(queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
        try:
            queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass
        # Drop the oldest event to make room; a slow client loses history, never
        # the newest state, and never blocks the producer.
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    def add_change_observer(self, observer: "Callable[[str, int], None]") -> None:
        """Register an in-process reaction to a committed write.

        This is how a database trigger reaches the application cache. A trigger
        cannot write into this process, so it announces (``pg_notify``); the
        listener turns that into :meth:`_apply_change`, which calls every
        observer. The cache registers here and evicts the affected entries, so a
        write made by *any* process invalidates this one's cache without a single
        polling query.

        Observers run synchronously inside the notification handler, so they must
        be fast and non-blocking — evicting dictionary entries, not awaiting I/O.
        An observer that raises is logged and skipped: a broken cache hook must
        never take down the stream that also feeds the browser.
        """
        self._change_observers.append(observer)

    def _notify_observers(self, table: str, version: int) -> None:
        for observer in self._change_observers:
            try:
                observer(table, version)
            except Exception:
                logger.exception("Change observer failed for table '%s'.", table)

    def _apply_change(self, table: str, version: int | None) -> None:
        if version is not None:
            self._versions[table] = int(version)
        else:
            self._versions[table] = self._versions.get(table, 0) + 1
        current = self._versions[table]
        # Invalidate before announcing. A browser told about a change will fetch
        # immediately; if the cache still held the pre-change value at that
        # moment the response would contradict the event that triggered it.
        self._notify_observers(table, current)
        self._publish({"type": "change", "table": table, "version": current})

    # -- postgres LISTEN ---------------------------------------------------

    def _dsn(self) -> str | None:
        url = self.database.url
        if not url.startswith(("postgresql+psycopg://", "postgresql://")):
            return None
        return url.replace("postgresql+psycopg://", "postgresql://", 1)

    async def _listen_loop(self) -> None:
        """Hold a dedicated connection on the change channel, reconnecting forever.

        This connection is deliberately outside the SQLAlchemy pool: it blocks on
        notifications for its whole life, so borrowing a pooled connection would
        starve request handling.
        """
        dsn = self._dsn()
        if dsn is None:
            return
        try:
            import psycopg
        except Exception:
            logger.warning("psycopg unavailable; falling back to polled change tracking.")
            await self._watch_loop()
            return

        delay = _RECONNECT_MIN_SECONDS
        while not self._stopping.is_set():
            try:
                async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as connection:
                    await connection.execute(f"LISTEN {CHANGE_CHANNEL}")
                    # Any write between losing the last connection and acquiring
                    # this one produced a notification nobody was listening for,
                    # so re-read the counters before trusting the push stream.
                    await self._read_versions_from_db()
                    self._push_active = True
                    delay = _RECONNECT_MIN_SECONDS
                    logger.info("Change feed listening on '%s'.", CHANGE_CHANNEL)
                    self._publish({"type": "ready", "versions": self.versions()})

                    async for notification in connection.notifies():
                        if self._stopping.is_set():
                            break
                        self._handle_notification(notification.payload)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("Change feed listener disconnected (%s); retrying in %.0fs.", error, delay)
            finally:
                self._push_active = False

            if self._stopping.is_set():
                return
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                delay = min(_RECONNECT_MAX_SECONDS, delay * 2)

    def _handle_notification(self, payload: str) -> None:
        try:
            parsed = json.loads(payload)
            table = str(parsed.get("table") or "")
            version = parsed.get("version")
        except Exception:
            # An unparseable payload still means *something* changed; bumping the
            # counter is the safe response (a redundant cache rebuild at worst).
            table, version = "", None
            logger.debug("Unparseable change notification: %r", payload)

        if not table:
            for tracked in TRACKED_TABLES:
                self._apply_change(tracked, None)
            return
        self._apply_change(table, int(version) if version is not None else None)

    # -- sqlite / fallback watch ------------------------------------------

    async def _watch_loop(self) -> None:
        """Poll the counters and publish diffs, for backends without NOTIFY.

        One small query per interval for the whole server, replacing one full
        session-index rebuild per client per interval.
        """
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_interval_seconds)
                return
            except asyncio.TimeoutError:
                pass
            previous = dict(self._versions)
            await self._read_versions_from_db()
            for table, version in self._versions.items():
                if previous.get(table, 0) != version:
                    self._publish({"type": "change", "table": table, "version": version})

    # -- SSE ---------------------------------------------------------------

    async def stream(self) -> AsyncIterator[str]:
        """Server-sent-event text stream of change announcements."""
        queue = self.subscribe()
        try:
            yield "retry: 3000\n\n"
            yield self._format_event(
                "ready",
                {"versions": self.versions(), "push": self._push_active},
            )
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25.0)
                except asyncio.TimeoutError:
                    # Comment frame: keeps proxies from reaping an idle stream
                    # without waking the browser's message handler.
                    yield ": keepalive\n\n"
                    continue
                if event.get("type") == "closed":
                    return
                yield self._format_event(str(event.get("type") or "change"), event)
        finally:
            self.unsubscribe(queue)

    @staticmethod
    def _format_event(name: str, payload: dict[str, Any]) -> str:
        return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
