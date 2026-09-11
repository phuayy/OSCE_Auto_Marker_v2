"""The single-value cache contract, in isolation.

The properties here are the ones that make it safe to cache a credential. Each
test names the failure it prevents, because every one of them is a way a cache
can quietly keep using a key its owner has revoked.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.snapshot_cache import UNCACHEABLE, SnapshotCache


class Counter:
    """A builder that records how often it actually ran."""

    def __init__(self, value: str = "v1") -> None:
        self.value = value
        self.builds = 0
        self.gate: asyncio.Event | None = None

    async def __call__(self) -> str:
        self.builds += 1
        if self.gate is not None:
            await self.gate.wait()
        return self.value


def test_a_repeated_read_under_one_token_builds_once() -> None:
    async def scenario() -> None:
        token = ["t1"]
        cache: SnapshotCache[str] = SnapshotCache("test", lambda: _resolve(token))
        build = Counter()

        assert await cache.get(build) == "v1"
        assert await cache.get(build) == "v1"
        assert await cache.get(build) == "v1"
        assert build.builds == 1
        assert cache.stats()["hits"] == 2

    asyncio.run(scenario())


def test_a_moved_token_rebuilds() -> None:
    """The backstop: the announcement was missed, the counter still moved."""

    async def scenario() -> None:
        token = ["t1"]
        cache: SnapshotCache[str] = SnapshotCache("test", lambda: _resolve(token))
        build = Counter("first")

        assert await cache.get(build) == "first"
        build.value = "second"
        assert await cache.get(build) == "first", "same token must serve the cached value"

        token[0] = "t2"
        assert await cache.get(build) == "second"
        assert build.builds == 2

    asyncio.run(scenario())


def test_invalidate_drops_the_value_even_when_the_token_has_not_moved() -> None:
    """The local-write path.

    With the PostgreSQL listener connected the token is answered from memory, so
    a process's own write does not move the counter it can see until its own
    notification arrives. Without this, the response to a rotation would be built
    from the value the rotation replaced.
    """

    async def scenario() -> None:
        token = ["t1"]
        cache: SnapshotCache[str] = SnapshotCache("test", lambda: _resolve(token))
        build = Counter("before")

        assert await cache.get(build) == "before"
        build.value = "after"
        cache.invalidate("written locally")

        assert await cache.get(build) == "after"
        assert cache.stats()["invalidations"] == 1

    asyncio.run(scenario())


def test_an_observer_only_reacts_to_its_own_table() -> None:
    async def scenario() -> None:
        token = ["t1"]
        cache: SnapshotCache[str] = SnapshotCache("test", lambda: _resolve(token))
        build = Counter("before")
        observe = cache.observer_for("provider_credentials")

        await cache.get(build)
        build.value = "after"

        observe("sessions", 7)
        assert await cache.get(build) == "before", "an unrelated table must not evict"

        observe("provider_credentials", 8)
        assert await cache.get(build) == "after"

    asyncio.run(scenario())


def test_an_unresolvable_token_disables_the_cache_entirely() -> None:
    """No way to learn about a change means no caching.

    Serving a credential nothing can invalidate is strictly worse than querying
    for it, so the cache stands down rather than guessing.
    """

    async def scenario() -> None:
        cache: SnapshotCache[str] = SnapshotCache("test", lambda: _resolve([UNCACHEABLE]))
        build = Counter()

        await cache.get(build)
        await cache.get(build)
        assert build.builds == 2
        assert cache.loaded is False

    asyncio.run(scenario())


def test_no_token_provider_means_no_caching() -> None:
    """The default a service gets when it was wired without a change feed."""

    async def scenario() -> None:
        cache: SnapshotCache[str] = SnapshotCache("test")
        build = Counter()

        await cache.get(build)
        await cache.get(build)
        assert build.builds == 2
        assert cache.enabled is False

    asyncio.run(scenario())


def test_a_failing_token_provider_falls_back_to_reading_rather_than_to_stale() -> None:
    async def scenario() -> None:
        async def broken() -> str:
            raise RuntimeError("counter unreadable")

        cache: SnapshotCache[str] = SnapshotCache("test", broken)
        build = Counter()

        assert await cache.get(build) == "v1"
        assert await cache.get(build) == "v1"
        assert build.builds == 2, "an unreadable counter must not be treated as unchanged"

    asyncio.run(scenario())


def test_concurrent_misses_build_once() -> None:
    """The stampede a scoring queue would otherwise cause.

    Ten runs dispatched together all miss on a cold cache; without the lock that
    is ten identical queries and ten decrypt passes.
    """

    async def scenario() -> None:
        token = ["t1"]
        cache: SnapshotCache[str] = SnapshotCache("test", lambda: _resolve(token))
        build = Counter()
        build.gate = asyncio.Event()

        waiters = [asyncio.create_task(cache.get(build)) for _ in range(10)]
        await asyncio.sleep(0)  # let them all reach the miss
        build.gate.set()
        results = await asyncio.gather(*waiters)

        assert results == ["v1"] * 10
        assert build.builds == 1

    asyncio.run(scenario())


def test_stats_report_counters_and_never_the_value() -> None:
    """A diagnostics endpoint must not become how a key escapes."""

    async def scenario() -> None:
        token = ["t1"]
        cache: SnapshotCache[str] = SnapshotCache("test", lambda: _resolve(token))
        await cache.get(Counter("sk-live-super-secret"))
        await cache.get(Counter("sk-live-super-secret"))

        stats = cache.stats()
        assert set(stats) == {"enabled", "loaded", "hits", "misses", "invalidations", "hitRate"}
        assert "sk-live-super-secret" not in repr(stats)
        assert stats == {
            "enabled": True,
            "loaded": True,
            "hits": 1,
            "misses": 1,
            "invalidations": 0,
            "hitRate": 0.5,
        }

    asyncio.run(scenario())


async def _resolve(holder: list) -> object:
    return holder[0]


@pytest.mark.parametrize("reason", ["", "rotated"])
def test_invalidating_an_empty_cache_is_a_no_op(reason: str) -> None:
    cache: SnapshotCache[str] = SnapshotCache("test")
    cache.invalidate(reason)
    assert cache.stats()["invalidations"] == 0
