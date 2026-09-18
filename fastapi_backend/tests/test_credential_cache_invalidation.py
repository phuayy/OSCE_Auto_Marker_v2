"""Caching the scoring hot path without ever serving a revoked credential.

Two values are resolved before every assessment, in every process: the model
selection (``app_settings``) and the provider API keys (``provider_credentials``).
Both used to be queried per run. Both are now cached, which is only defensible
because the database itself announces a change -- a trigger bumps
``table_versions`` and fires ``pg_notify``, and ``ChangeFeedService`` turns that
into an eviction everywhere.

So the tests that matter are not "the cache is fast" but "the cache cannot
outlive a rotation":

* a key rotated in *another* process reaches this one, and the next scoring run
  authenticates with the new one;
* revoking a key stops it being served immediately, not eventually;
* a missed announcement is still caught, by the counter;
* a deployment where nothing can announce does not cache at all;
* and, given all that holds, the query budget per run is what we claim.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, text

from app.database.change_tracking import TRACKED_TABLES, install_change_tracking
from app.database.orm import OrmDatabase
from app.repositories.app_settings_repository import AppSettingsRepository
from app.repositories.provider_credential_repository import ProviderCredentialRepository
from app.repositories.user_settings_repository import UserSettingsRepository
from app.services.change_feed_service import ChangeFeedService
from app.services.llm_settings_service import LLMSettingsService
from app.services.preferences_service import PreferencesService
from app.services.provider_credential_service import ProviderCredentialService


ORIGINAL_KEY = "sk-original-abcdefghijkl"
ROTATED_KEY = "sk-rotated-mnopqrstuvwx"
MASTER_SECRET = "deployment-auth-secret"

# How long a test will wait for the change feed's watch loop to notice a write.
# Bounded wait rather than a fixed sleep: the loop polls, so the test should
# finish as soon as it has, and fail loudly if it never does.
PROPAGATION_TIMEOUT = 5.0
WATCH_INTERVAL = 0.05


@pytest.fixture(autouse=True)
def _no_ambient_vendor_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run against a machine with no vendor keys exported, so "which key would
    be sent" is decided entirely by what these tests store."""
    from app.llm import registry

    for descriptor in registry.DESCRIPTORS.values():
        for name in descriptor.api_key_env:
            monkeypatch.delenv(name, raising=False)


class Harness:
    """One database, its triggers, and the services that cache in front of it."""

    def __init__(self, database: OrmDatabase, changes: ChangeFeedService) -> None:
        self.database = database
        self.changes = changes
        self.credential_repository = ProviderCredentialRepository(database)
        self.store = ProviderCredentialService(
            self.credential_repository,
            master_key_source=lambda: MASTER_SECRET,
            changes=changes,
        )
        self.app_settings = AppSettingsRepository(database, changes=changes)
        self.user_settings = UserSettingsRepository(database, changes=changes)
        self.preferences = PreferencesService(self.app_settings, self.user_settings)
        self.service = LLMSettingsService(self.preferences, credential_store=self.store)
        self.selects: list[str] = []
        self._recording = False

        @event.listens_for(database.engine.sync_engine, "before_cursor_execute")
        def _record(conn, cursor, statement, *_args):  # noqa: ANN001, ANN202
            if self._recording and statement.lstrip().upper().startswith("SELECT"):
                self.selects.append(" ".join(statement.split()))

    def record(self) -> None:
        self.selects.clear()
        self._recording = True

    def stop_recording(self) -> list[str]:
        self._recording = False
        return list(self.selects)

    def simulate_listener_connected(self) -> None:
        """Put the feed in the state a connected PostgreSQL listener creates:
        counters held in memory, so a token check costs no query."""
        self.changes._push_active = True

    def deliver_notification(self, table: str, version: int) -> None:
        """Hand the feed the payload its PostgreSQL listener would receive.

        This is the exact entry point ``_listen_loop`` calls for every
        ``pg_notify``, so driving it directly tests the real wiring without
        needing a PostgreSQL server.
        """
        self.changes._handle_notification(json.dumps({"table": table, "version": version}))

    async def scoring_key(self) -> str:
        """The key a scoring subprocess would be launched with."""
        return (await self.service.subprocess_env()).get("DEEPSEEK_API_KEY", "")


async def build_harness(tmp_path: Path, *, start_feed: bool = False) -> Harness:
    database = OrmDatabase(tmp_path / "app.sqlite3")
    await database.initialize()
    # The triggers under test: without them nothing announces, and the caches
    # would have only the counter to go on.
    await install_change_tracking(database.engine)
    changes = ChangeFeedService(database, poll_interval_seconds=WATCH_INTERVAL)
    harness = Harness(database, changes)
    if start_feed:
        await changes.start(push_enabled=False)
    await harness.store.set_key("deepseek", ORIGINAL_KEY)
    await harness.app_settings.set_values(
        {"llmPrimary": {"providerId": "deepseek", "model": "deepseek-chat"}, "llmFallbacks": []}
    )
    return harness


async def wait_for(condition, timeout: float = PROPAGATION_TIMEOUT) -> None:
    """Poll until ``condition()`` is truthy, or fail with what it last saw."""

    async def poll() -> None:
        while not await condition():
            await asyncio.sleep(WATCH_INTERVAL / 2)

    await asyncio.wait_for(poll(), timeout=timeout)


# --- tracking ---------------------------------------------------------------


def test_both_cached_tables_are_change_tracked() -> None:
    """The caches are only safe because these two announce their writes."""
    assert "provider_credentials" in TRACKED_TABLES
    assert "app_settings" in TRACKED_TABLES


def test_a_write_bumps_the_table_counter(tmp_path: Path) -> None:
    """The trigger half of the contract, checked against a real database."""

    async def scenario() -> None:
        harness = await build_harness(tmp_path)
        async with harness.database.session() as db:
            before = (
                await db.execute(
                    text("SELECT version FROM table_versions WHERE table_name = 'provider_credentials'")
                )
            ).scalar_one()

        await harness.store.set_key("openai", ROTATED_KEY)

        async with harness.database.session() as db:
            after = (
                await db.execute(
                    text("SELECT version FROM table_versions WHERE table_name = 'provider_credentials'")
                )
            ).scalar_one()
        assert after > before

    asyncio.run(scenario())


# --- rotation reaches every process -----------------------------------------


def test_a_rotation_in_another_process_reaches_this_one(tmp_path: Path) -> None:
    """The headline behaviour: the API rotates a key, the worker starts using it.

    The second service stands in for the Hatchet worker — its own cache, its own
    connection, sharing only the database. It is never told about the rotation by
    the code that performs it; the trigger is what closes the loop.
    """

    async def scenario() -> None:
        harness = await build_harness(tmp_path, start_feed=True)
        try:
            assert await harness.scoring_key() == ORIGINAL_KEY

            rotator = ProviderCredentialService(
                ProviderCredentialRepository(harness.database),
                master_key_source=lambda: MASTER_SECRET,
            )
            await rotator.set_key("deepseek", ROTATED_KEY, actor="admin")

            await wait_for(lambda: _is(harness.scoring_key(), ROTATED_KEY))
        finally:
            await harness.changes.stop()

    asyncio.run(scenario())


def test_a_postgres_notification_evicts_without_any_polling(tmp_path: Path) -> None:
    """The push path, driven through the handler the listener calls.

    On PostgreSQL nothing polls: the rotation arrives as a ``pg_notify`` payload
    and the cache is dropped inside the handler.
    """

    async def scenario() -> None:
        harness = await build_harness(tmp_path)
        harness.simulate_listener_connected()
        assert await harness.scoring_key() == ORIGINAL_KEY

        rotator = ProviderCredentialService(
            ProviderCredentialRepository(harness.database),
            master_key_source=lambda: MASTER_SECRET,
        )
        await rotator.set_key("deepseek", ROTATED_KEY)

        # Still cached: with the listener connected the counter this process can
        # see has not moved, and no announcement has arrived yet.
        assert await harness.scoring_key() == ORIGINAL_KEY

        harness.deliver_notification("provider_credentials", 99)
        assert await harness.scoring_key() == ROTATED_KEY

    asyncio.run(scenario())


def test_a_missed_announcement_is_still_caught_by_the_counter(tmp_path: Path) -> None:
    """The backstop, for the window while a listener is reconnecting.

    No notification is delivered here at all. The counter moved, so the next
    read rebuilds anyway — slower to notice, never wrong.
    """

    async def scenario() -> None:
        harness = await build_harness(tmp_path)
        assert await harness.scoring_key() == ORIGINAL_KEY

        rotator = ProviderCredentialService(
            ProviderCredentialRepository(harness.database),
            master_key_source=lambda: MASTER_SECRET,
        )
        await rotator.set_key("deepseek", ROTATED_KEY)

        assert await harness.scoring_key() == ROTATED_KEY

    asyncio.run(scenario())


def test_a_local_rotation_applies_to_the_very_next_call(tmp_path: Path) -> None:
    """Read-your-writes, even with the listener holding the counter in memory."""

    async def scenario() -> None:
        harness = await build_harness(tmp_path)
        harness.simulate_listener_connected()
        assert await harness.scoring_key() == ORIGINAL_KEY

        await harness.store.set_key("deepseek", ROTATED_KEY, actor="admin")

        assert await harness.scoring_key() == ROTATED_KEY

    asyncio.run(scenario())


def test_revoking_a_key_stops_it_being_served_immediately(tmp_path: Path) -> None:
    """The case where a stale cache does real damage."""

    async def scenario() -> None:
        harness = await build_harness(tmp_path)
        harness.simulate_listener_connected()
        assert await harness.scoring_key() == ORIGINAL_KEY

        await harness.store.clear_key("deepseek", actor="admin")

        assert await harness.scoring_key() == ""
        assert (await harness.service.credential_sources())["deepseek"] == "none"

    asyncio.run(scenario())


def test_a_model_change_applies_to_the_next_run_without_a_restart(tmp_path: Path) -> None:
    """The settings cache keeps the contract the uncached read used to provide."""

    async def scenario() -> None:
        harness = await build_harness(tmp_path)
        harness.simulate_listener_connected()
        await harness.store.set_key("openai", "sk-openai-abcdefghijkl")

        assert (await harness.service.routing()).primary.provider_id == "deepseek"

        await harness.app_settings.set_values(
            {"llmPrimary": {"providerId": "openai", "model": "gpt-4.1"}, "llmFallbacks": []}
        )

        assert (await harness.service.routing()).primary.provider_id == "openai"
        assert (await harness.service.subprocess_env())["OPENAI_API_KEY"] == "sk-openai-abcdefghijkl"

    asyncio.run(scenario())


def test_a_settings_change_in_another_process_reaches_this_one(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await build_harness(tmp_path, start_feed=True)
        try:
            await harness.store.set_key("openai", "sk-openai-abcdefghijkl")
            assert (await harness.service.routing()).primary.provider_id == "deepseek"

            other = AppSettingsRepository(harness.database)
            await other.set_values(
                {"llmPrimary": {"providerId": "openai", "model": "gpt-4.1"}, "llmFallbacks": []}
            )

            await wait_for(lambda: _routes_to(harness.service, "openai"))
        finally:
            await harness.changes.stop()

    asyncio.run(scenario())


def test_a_recorded_test_result_shows_up_without_a_reload_delay(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await build_harness(tmp_path)
        harness.simulate_listener_connected()
        assert (await harness.service.credential_statuses())["deepseek"]["lastTestOk"] is None

        await harness.store.record_test("deepseek", ok=True)

        assert (await harness.service.credential_statuses())["deepseek"]["lastTestOk"] is True

    asyncio.run(scenario())


# --- safety defaults --------------------------------------------------------


def test_without_a_change_feed_nothing_is_cached(tmp_path: Path) -> None:
    """A service that cannot be told about a rotation must not hold one.

    This is the wiring a test double or a partially-built container produces,
    and it degrades to the old per-call read rather than to a stale key.
    """

    async def scenario() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        await database.initialize()
        store = ProviderCredentialService(
            ProviderCredentialRepository(database), master_key_source=lambda: MASTER_SECRET
        )
        await store.set_key("deepseek", ORIGINAL_KEY)

        assert (await store.api_keys())["deepseek"] == ORIGINAL_KEY
        assert store.cache_stats()["enabled"] is False
        assert store.cache_stats()["loaded"] is False

        # A rotation by anyone is visible on the very next read, because there
        # is no cache to go stale.
        other = ProviderCredentialService(
            ProviderCredentialRepository(database), master_key_source=lambda: MASTER_SECRET
        )
        await other.set_key("deepseek", ROTATED_KEY)
        assert (await store.api_keys())["deepseek"] == ROTATED_KEY

    asyncio.run(scenario())


def test_cache_statistics_expose_counters_only(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = await build_harness(tmp_path)
        harness.simulate_listener_connected()
        await harness.service.describe()

        stats = harness.service.credential_cache_stats()
        assert set(stats) == {"enabled", "loaded", "hits", "misses", "invalidations", "hitRate"}
        assert ORIGINAL_KEY not in json.dumps(stats)
        assert "deepseek" not in json.dumps(stats)

    asyncio.run(scenario())


# --- query budget -----------------------------------------------------------


def test_a_warm_scoring_handoff_costs_no_queries_at_all(tmp_path: Path) -> None:
    """What the whole exercise was for.

    With a connected listener both caches answer from memory, so resolving the
    model and the credentials for a run touches the database zero times. Fifty
    runs in a row cost the same as one.
    """

    async def scenario() -> None:
        harness = await build_harness(tmp_path)
        harness.simulate_listener_connected()
        await harness.service.subprocess_env()  # warm both caches

        harness.record()
        for _ in range(50):
            await harness.service.subprocess_env()
        assert harness.stop_recording() == []

    asyncio.run(scenario())


def test_a_cold_settings_screen_reads_each_table_once(tmp_path: Path) -> None:
    """``describe()`` used to read the credential table four times over.

    Every projection it renders — availability, sources, masked metadata — is
    derived from one snapshot now, so a cold load is one read per table rather
    than one read per consumer.
    """

    async def scenario() -> None:
        harness = await build_harness(tmp_path)
        harness.simulate_listener_connected()
        harness.store.invalidate("test")
        harness.app_settings.invalidate("test")

        harness.record()
        await harness.service.describe()
        statements = harness.stop_recording()

        assert sum("provider_credentials" in s for s in statements) == 1, statements
        assert sum("app_settings" in s for s in statements) == 1, statements

    asyncio.run(scenario())


def test_the_degraded_path_reads_counters_not_credentials(tmp_path: Path) -> None:
    """With no listener the caches fall back to comparing counters.

    That still costs a query per resolve, but it is a handful of ``table_versions``
    rows rather than every credential row plus a decrypt pass for each.
    """

    async def scenario() -> None:
        harness = await build_harness(tmp_path)
        await harness.service.subprocess_env()  # warm

        harness.record()
        await harness.service.subprocess_env()
        statements = harness.stop_recording()

        assert statements, "the degraded path must still verify freshness"
        assert all("table_versions" in s for s in statements), statements
        assert not any("provider_credentials" in s for s in statements), statements

    asyncio.run(scenario())


def test_concurrent_scoring_starts_share_one_credential_read(tmp_path: Path) -> None:
    """Ten runs dispatched together must not each decrypt the table."""

    async def scenario() -> None:
        harness = await build_harness(tmp_path)
        harness.simulate_listener_connected()
        harness.store.invalidate("test")
        harness.app_settings.invalidate("test")

        harness.record()
        await asyncio.gather(*(harness.service.subprocess_env() for _ in range(10)))
        statements = harness.stop_recording()

        assert sum("provider_credentials" in s for s in statements) == 1, statements
        assert sum("app_settings" in s for s in statements) == 1, statements

    asyncio.run(scenario())


async def _is(awaitable, expected: Any) -> bool:
    return await awaitable == expected


async def _routes_to(service: LLMSettingsService, provider_id: str) -> bool:
    return (await service.routing()).primary.provider_id == provider_id
