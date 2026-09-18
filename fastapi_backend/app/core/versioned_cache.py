from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Hashable, Iterable


logger = logging.getLogger(__name__)


@dataclass
class _Entry:
    token: Hashable
    value: Any
    # Tables this entry was built from. A committed write to any of them drops
    # the entry, so invalidation is driven by the database rather than a clock.
    tables: frozenset[str] = field(default_factory=frozenset)


class VersionedCache:
    """Read-through cache invalidated by database change announcements.

    Two mechanisms guard freshness, and they cover different failure modes:

    * **Push invalidation** — :meth:`invalidate_tables` is called from the change
      feed the moment a tracked table is written. On PostgreSQL that originates
      from a trigger's ``pg_notify``, so a write by *any* process (notably the
      Hatchet worker) evicts this process's entry within milliseconds, with no
      polling and no database read to discover it.
    * **Token comparison** — every entry also records the change-counter token it
      was built from, and a hit requires that token to still match. This is the
      backstop for anything push cannot cover: the window before the listener
      reconnects, a SQLite deployment with no ``NOTIFY`` at all, or an event
      dropped under back-pressure.

    Either alone would be wrong. Push alone silently serves stale data whenever an
    announcement is missed; tokens alone force a counter read on every request.
    Together, the common path costs no database round trip and the uncommon path
    is merely slower.

    A plain TTL cache was not an option: sessions are written by a separate
    process, so this one cannot know its copy went stale, and any TTL trades
    serving wrong data against doing the expensive work anyway.
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self._entries: dict[str, _Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()
        self._enabled = enabled
        self.hits = 0
        self.misses = 0
        self.invalidations = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def get_or_build(
        self,
        key: str,
        token: Hashable,
        builder: Callable[[], Awaitable[Any]],
        *,
        tables: Iterable[str] | None = None,
    ) -> Any:
        """Return the cached value for ``key``, or build and store it.

        A hit requires both that the entry exists and that it was built from the
        same ``token``. ``tables`` records which tables the value derives from so
        a later write to one of them can evict it directly.

        Concurrent callers that miss on the same key are serialised, so a burst
        of requests arriving right after an invalidation runs ``builder`` once
        rather than once per caller — the stampede this cache exists to prevent.
        """
        if not self._enabled:
            return await builder()

        entry = self._entries.get(key)
        if entry is not None and entry.token == token:
            self.hits += 1
            return entry.value

        lock = await self._lock_for(key)
        async with lock:
            # Re-check: another caller may have rebuilt this key while we waited.
            entry = self._entries.get(key)
            if entry is not None and entry.token == token:
                self.hits += 1
                return entry.value

            self.misses += 1
            value = await builder()
            if len(self._entries) >= 256 and key not in self._entries:
                self._entries.pop(next(iter(self._entries)))
            self._entries[key] = _Entry(
                token=token,
                value=value,
                tables=frozenset(tables or ()),
            )
            return value

    async def _lock_for(self, key: str) -> asyncio.Lock:
        key = str(hash(key) % 64)
        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    def invalidate(self, key: str | None = None) -> None:
        """Drop one entry, or every entry when ``key`` is None."""
        if key is None:
            self._entries.clear()
            return
        self._entries.pop(key, None)

    def invalidate_tables(self, tables: Iterable[str]) -> int:
        """Drop every entry built from any of ``tables``. Returns how many.

        This is the push path, called from the change feed. It is synchronous and
        allocation-light on purpose: it runs inside the notification handler that
        also feeds the browser's SSE stream, so it must never await or block.

        An entry that declared no tables is never evicted here — it opted out of
        push invalidation and relies on its token alone.
        """
        if not self._entries:
            return 0
        targets = frozenset(tables)
        if not targets:
            return 0
        doomed = [
            key
            for key, entry in self._entries.items()
            if entry.tables and not entry.tables.isdisjoint(targets)
        ]
        for key in doomed:
            self._entries.pop(key, None)
        if doomed:
            self.invalidations += len(doomed)
            logger.debug("Cache invalidated %d entry/entries for %s.", len(doomed), ", ".join(sorted(targets)))
        return len(doomed)

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "entries": len(self._entries),
            "invalidations": self.invalidations,
            "enabled": self._enabled,
            # Rounded so the readiness/diagnostics payload stays stable-ish
            # across requests instead of jittering in the last decimal.
            "hitRate": round(self.hits / total, 3) if total else 0.0,
        }
