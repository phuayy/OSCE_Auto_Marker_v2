"""Unit cover for the read-through cache and its push invalidation."""

from __future__ import annotations

import asyncio

from app.core.versioned_cache import VersionedCache


def _counting_builder(values: list):
    """A builder that records how many times it ran — i.e. how many times the
    database would have been hit."""
    calls = {"count": 0}

    async def build():
        calls["count"] += 1
        return values[min(calls["count"] - 1, len(values) - 1)]

    return build, calls


# --- read path -------------------------------------------------------------


def test_same_token_is_served_from_cache_without_rebuilding() -> None:
    async def scenario() -> None:
        cache = VersionedCache()
        build, calls = _counting_builder(["first", "second"])

        assert await cache.get_or_build("k", ("v", 1), build) == "first"
        assert await cache.get_or_build("k", ("v", 1), build) == "first"
        assert await cache.get_or_build("k", ("v", 1), build) == "first"

        assert calls["count"] == 1, "a hit must not touch the database"
        assert cache.hits == 2
        assert cache.misses == 1

    asyncio.run(scenario())


def test_changed_token_rebuilds() -> None:
    """The backstop for a missed push announcement: a moved counter alone is
    enough to invalidate."""

    async def scenario() -> None:
        cache = VersionedCache()
        build, calls = _counting_builder(["first", "second"])

        assert await cache.get_or_build("k", ("v", 1), build) == "first"
        assert await cache.get_or_build("k", ("v", 2), build) == "second"
        assert calls["count"] == 2

    asyncio.run(scenario())


def test_keys_are_independent() -> None:
    async def scenario() -> None:
        cache = VersionedCache()
        build_a, calls_a = _counting_builder(["a"])
        build_b, calls_b = _counting_builder(["b"])

        assert await cache.get_or_build("a", ("v", 1), build_a) == "a"
        assert await cache.get_or_build("b", ("v", 1), build_b) == "b"
        assert await cache.get_or_build("a", ("v", 1), build_a) == "a"

        assert calls_a["count"] == 1
        assert calls_b["count"] == 1

    asyncio.run(scenario())


def test_concurrent_misses_build_once() -> None:
    """Single-flight. Without it, a reconnect storm right after an invalidation
    would run the expensive projection once per client."""

    async def scenario() -> None:
        cache = VersionedCache()
        calls = {"count": 0}

        async def slow_build():
            calls["count"] += 1
            await asyncio.sleep(0.05)
            return "value"

        results = await asyncio.gather(
            *(cache.get_or_build("k", ("v", 1), slow_build) for _ in range(10))
        )

        assert results == ["value"] * 10
        assert calls["count"] == 1

    asyncio.run(scenario())


def test_disabled_cache_always_builds() -> None:
    """CACHE_ENABLED=false must be a true bypass, not a silent no-op."""

    async def scenario() -> None:
        cache = VersionedCache(enabled=False)
        build, calls = _counting_builder(["a", "b", "c"])

        assert await cache.get_or_build("k", ("v", 1), build) == "a"
        assert await cache.get_or_build("k", ("v", 1), build) == "b"

        assert calls["count"] == 2
        assert cache.stats()["enabled"] is False

    asyncio.run(scenario())


# --- push invalidation -----------------------------------------------------


def test_invalidate_tables_drops_dependent_entries() -> None:
    async def scenario() -> None:
        cache = VersionedCache()
        build, calls = _counting_builder(["first", "second"])

        await cache.get_or_build("k", ("v", 1), build, tables=("sessions",))
        assert cache.invalidate_tables(("sessions",)) == 1

        # Same token as before — only the push invalidation forces the rebuild,
        # which is the whole point: the counter read is skipped entirely.
        assert await cache.get_or_build("k", ("v", 1), build) == "second"
        assert calls["count"] == 2

    asyncio.run(scenario())


def test_invalidate_tables_leaves_unrelated_entries() -> None:
    async def scenario() -> None:
        cache = VersionedCache()
        build_s, calls_s = _counting_builder(["sessions-value"])
        build_n, calls_n = _counting_builder(["notifications-value"])

        await cache.get_or_build("s", ("v", 1), build_s, tables=("sessions",))
        await cache.get_or_build("n", ("v", 1), build_n, tables=("notifications",))

        assert cache.invalidate_tables(("sessions",)) == 1

        await cache.get_or_build("n", ("v", 1), build_n)
        assert calls_n["count"] == 1, "an unrelated table's entry must survive"

    asyncio.run(scenario())


def test_entry_without_declared_tables_is_not_push_evicted() -> None:
    """Opting out of push invalidation must be possible; such an entry relies on
    its token alone."""

    async def scenario() -> None:
        cache = VersionedCache()
        build, calls = _counting_builder(["value"])

        await cache.get_or_build("k", ("v", 1), build)  # no tables declared
        assert cache.invalidate_tables(("sessions", "notifications")) == 0

        await cache.get_or_build("k", ("v", 1), build)
        assert calls["count"] == 1

    asyncio.run(scenario())


def test_invalidate_tables_on_empty_cache_is_safe() -> None:
    cache = VersionedCache()
    assert cache.invalidate_tables(("sessions",)) == 0
    assert cache.invalidate_tables(()) == 0


def test_invalidate_all_and_single_key() -> None:
    async def scenario() -> None:
        cache = VersionedCache()
        build, calls = _counting_builder(["a", "b", "c"])

        await cache.get_or_build("k", ("v", 1), build, tables=("sessions",))
        cache.invalidate("k")
        await cache.get_or_build("k", ("v", 1), build, tables=("sessions",))
        assert calls["count"] == 2

        cache.invalidate()
        await cache.get_or_build("k", ("v", 1), build, tables=("sessions",))
        assert calls["count"] == 3

    asyncio.run(scenario())


def test_stats_report_hit_rate_and_invalidations() -> None:
    async def scenario() -> None:
        cache = VersionedCache()
        build, _calls = _counting_builder(["value"])

        await cache.get_or_build("k", ("v", 1), build, tables=("sessions",))  # miss
        await cache.get_or_build("k", ("v", 1), build, tables=("sessions",))  # hit
        cache.invalidate_tables(("sessions",))

        stats = cache.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["invalidations"] == 1
        assert stats["hitRate"] == 0.5
        assert stats["entries"] == 0

    asyncio.run(scenario())
