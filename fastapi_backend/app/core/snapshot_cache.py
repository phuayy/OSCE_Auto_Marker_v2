"""A single cached value, invalidated by the database rather than by a clock.

:class:`~app.core.versioned_cache.VersionedCache` solves the keyed case: many
independent projections, each evicted when one of the tables it was built from is
written. This is the degenerate one — a component that reads *all* of one small,
rarely-written table on *every* request and wants to stop.

The two hot paths in scoring have exactly that shape. ``app_settings`` holds the
model selection and the transcription engine; ``provider_credentials`` holds the
API keys. Both are read on every run, in every process, and written a handful of
times a year. Reading them per run is the cost; caching them is only safe if a
write anywhere reaches every process, which is what the change-tracking triggers
already provide.

Freshness rests on two mechanisms, and both are needed:

* **Push** — the table's trigger bumps ``table_versions`` and (on PostgreSQL)
  fires ``pg_notify``; ``ChangeFeedService`` turns that into an observer call in
  every listening process, so a write by the API evicts the Hatchet worker's copy
  within milliseconds.
* **Token comparison** — the entry records the counter it was built from, and a
  hit requires that counter to still match. This covers what push cannot: the
  window while a listener reconnects, a SQLite deployment with no ``NOTIFY``, an
  event dropped under back-pressure.

Push alone serves stale data whenever an announcement is missed. Tokens alone
cost a counter read per request. Together, the common path on PostgreSQL costs
no database round trip at all, because the listener keeps the counters in memory.

Two rules callers must honour, both learned the hard way:

* **Invalidate on your own writes.** With the listener connected the token comes
  from memory, so this process's counter does not move until its own notification
  arrives — and the response to a write is usually built from the very value the
  write changed. Local eviction is a correctness requirement, not a speed-up.
* **A token that cannot be established disables the cache.** Serving a value with
  no way to learn it changed is worse than reading the table. That is doubly true
  when the value is a credential: the failure mode is a revoked key still in use.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Generic, Hashable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Returned by a token provider that cannot establish freshness. Caching under it
# would mean never noticing a change, so it bypasses the cache entirely.
UNCACHEABLE = object()

TokenProvider = Callable[[], Awaitable[Hashable]]


@dataclass
class _Entry(Generic[T]):
    token: Hashable
    value: T


class SnapshotCache(Generic[T]):
    """One value, rebuilt when the database says the table behind it changed.

    ``name`` appears in debug logs only. ``token_provider`` returns a comparable
    snapshot of the relevant change counters, or :data:`UNCACHEABLE`.
    """

    def __init__(self, name: str, token_provider: TokenProvider | None = None) -> None:
        self.name = name
        self._token_provider = token_provider
        self._entry: _Entry[T] | None = None
        # Serialises rebuilds so a burst of callers arriving on a cold cache does
        # one build between them, not one each — the stampede this exists to
        # prevent, and the reason a scoring queue starting ten runs at once does
        # not produce ten identical queries.
        self._lock = asyncio.Lock()
        self.hits = 0
        self.misses = 0
        self.invalidations = 0

    @property
    def enabled(self) -> bool:
        """False when nothing can announce a change, so nothing is cached."""
        return self._token_provider is not None

    @property
    def loaded(self) -> bool:
        return self._entry is not None

    async def get(self, build: Callable[[], Awaitable[T]]) -> T:
        """The cached value, or ``build()``'s result stored for next time."""
        token = await self._token()
        if token is UNCACHEABLE:
            return await build()

        entry = self._entry
        if entry is not None and entry.token == token:
            self.hits += 1
            return entry.value

        async with self._lock:
            # Re-check: another caller may have rebuilt while we queued.
            entry = self._entry
            if entry is not None and entry.token == token:
                self.hits += 1
                return entry.value
            self.misses += 1
            # The token is read *before* the value, never after. A write landing
            # between the two yields a value newer than its token, costing one
            # redundant rebuild. Reading the token afterwards would yield the
            # opposite — a value older than its token, cached as current — which
            # is the stale read this class exists to make impossible.
            value = await build()
            self._entry = _Entry(token=token, value=value)
            return value

    def invalidate(self, reason: str = "") -> None:
        """Drop the cached value. Synchronous and non-blocking, so it is safe to
        call from the change-feed handler and from sync code alike."""
        if self._entry is None:
            return
        self._entry = None
        self.invalidations += 1
        logger.debug("%s cache invalidated: %s", self.name, reason or "unspecified")

    def observer_for(self, table: str) -> Callable[[str, int], None]:
        """A ``ChangeFeedService.add_change_observer`` callback for one table."""

        def _observe(changed_table: str, version: int) -> None:
            if changed_table == table:
                self.invalidate(f"{changed_table} changed (version {version})")

        return _observe

    async def _token(self) -> Hashable:
        if self._token_provider is None:
            return UNCACHEABLE
        try:
            return await self._token_provider()
        except Exception:  # pragma: no cover - defensive
            # Unable to establish freshness means unable to cache. Falling back
            # to a live read is slower; trusting a stale value is wrong.
            logger.warning(
                "%s cache could not read its change token; bypassing the cache.",
                self.name,
                exc_info=True,
            )
            return UNCACHEABLE

    def stats(self) -> dict[str, Any]:
        """Counters for the readiness payload.

        Deliberately counts only. One of these caches holds API keys, and a
        diagnostics endpoint must not become the way they leak — not even as a
        per-key breakdown.
        """
        total = self.hits + self.misses
        return {
            "enabled": self.enabled,
            "loaded": self.loaded,
            "hits": self.hits,
            "misses": self.misses,
            "invalidations": self.invalidations,
            "hitRate": round(self.hits / total, 3) if total else 0.0,
        }
