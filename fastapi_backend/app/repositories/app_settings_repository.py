from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.core.snapshot_cache import SnapshotCache
from app.database.models import AppSettingRecord, utc_now
from app.database.orm import OrmDatabase

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from app.services.change_feed_service import ChangeFeedService


logger = logging.getLogger(__name__)

# Physical table name, as the change trigger reports it.
SETTINGS_TABLE = "app_settings"

LLM_PREPROCESS_KEY = "llmTranscriptPreprocess"
# Selected transcription engine id, and its per-engine option overrides keyed
# by engine id — options are kept per engine so switching back and forth does
# not lose the tuning done for either.
TRANSCRIPTION_ENGINE_KEY = "transcriptionEngine"
TRANSCRIPTION_OPTIONS_KEY = "transcriptionEngineOptions"
# The scoring model the operator picked, and the ordered list to try when it
# fails. Each entry is {"providerId": ..., "model": ...}. API keys are NOT
# stored here — they stay in the deployment's environment, because this table
# is dumped verbatim to anyone who can open the settings screen.
LLM_PRIMARY_KEY = "llmPrimary"
LLM_FALLBACKS_KEY = "llmFallbacks"
# How content is marked: "single" (one model, the routing above) or "panel"
# (several markers plus an adjudicator, described by the panel key). The panel
# value has the shape of app.llm.panel.PanelConfig.to_public() minus the
# schema field: {"markers": [...], "adjudicator": {...}, "tieBreak": "..."}.
LLM_MARKING_MODE_KEY = "llmMarkingMode"
LLM_PANEL_KEY = "llmPanel"

# Known settings and their defaults. GET merges stored rows over these so the
# API response shape stays stable as settings are added. An empty engine id
# means "whatever this deployment configured", which the router resolves.
DEFAULT_SETTINGS: dict[str, Any] = {
    LLM_PREPROCESS_KEY: False,
    TRANSCRIPTION_ENGINE_KEY: "",
    TRANSCRIPTION_OPTIONS_KEY: {},
    # An empty primary means "whatever this deployment's default provider is",
    # which LLMSettingsService resolves. Written this way so a fresh install and
    # an install that predates the LLM router behave identically.
    LLM_PRIMARY_KEY: {},
    LLM_FALLBACKS_KEY: [],
    # Single-model marking is what every deployment ran before the panel
    # existed, so a missing row must mean exactly that.
    LLM_MARKING_MODE_KEY: "single",
    LLM_PANEL_KEY: {},
}


class AppSettingsRepository:
    """Reads and writes the settings table, and owns the cache in front of it.

    Every per-run selection lives here — the scoring model, the transcription
    engine, the preprocess toggle — and each one used to be its own query on
    every run, in every process. They are all projections of one small table
    that is written a few times a year, so the reads collapse into one cached
    snapshot.

    The cache lives on the repository rather than on its callers because this is
    the only component that sees both the reads and the writes, and therefore the
    only one that can guarantee read-your-writes: :meth:`set_values` evicts
    directly. Cross-process freshness comes from the change feed — a settings
    write fires the table's trigger, and the announcement evicts the Hatchet
    worker's copy — so the "applies to the next run everywhere, with no restart"
    contract is unchanged. Only the mechanism moves, from asking every time to
    being told when it matters.

    Without a change feed the cache disables itself and every read goes to the
    database, exactly as before.
    """

    def __init__(self, database: OrmDatabase, *, changes: "ChangeFeedService | None" = None) -> None:
        self.database = database
        self._cache: SnapshotCache[dict[str, Any]] = SnapshotCache(
            "app-settings",
            token_provider=(
                (lambda: changes.token((SETTINGS_TABLE,))) if changes is not None else None
            ),
        )
        if changes is not None:
            changes.add_change_observer(self._cache.observer_for(SETTINGS_TABLE))

    async def _snapshot(self) -> dict[str, Any]:
        """Every setting, defaults merged under whatever is stored.

        One query serves every accessor below. ``get_all`` returns a copy so a
        caller mutating its result cannot corrupt the cached snapshot.
        """
        return await self._cache.get(self._read_all)

    async def _read_all(self) -> dict[str, Any]:
        async with self.database.session() as db:
            records = (await db.scalars(select(AppSettingRecord))).all()
        stored = {record.key: record.value for record in records}
        return {**DEFAULT_SETTINGS, **stored}

    def invalidate(self, reason: str = "") -> None:
        self._cache.invalidate(reason)

    def cache_stats(self) -> dict[str, Any]:
        return self._cache.stats()

    async def get_all(self) -> dict[str, Any]:
        return dict(await self._snapshot())

    async def set_values(self, values: dict[str, Any]) -> dict[str, Any]:
        async with self.database.transaction() as db:
            for key, value in values.items():
                record = await db.get(AppSettingRecord, key)
                if record is None:
                    db.add(AppSettingRecord(key=key, value=value))
                else:
                    record.value = value
                    record.updated_at = utc_now()
        # Evicted here rather than left to the trigger's announcement. With the
        # PostgreSQL listener connected the token is served from memory, so this
        # process's counter does not move until its own notification arrives —
        # and the response to this very call is built from the snapshot below.
        # Waiting would answer a settings save with the values it replaced.
        self.invalidate("settings written")
        return await self.get_all()

    async def transcription_selection(self) -> tuple[str, dict[str, Any]]:
        """Live read for the pipeline: the selected engine id and the per-engine
        option map, keyed by engine id.

        Returns an empty id when nothing is stored so the router applies the
        deployment default; the caller selects the option bag for whichever
        engine it resolves to. A read failure is *not* swallowed here — unlike
        the preprocess toggle, silently transcribing with a different engine
        than the operator selected would change the run's output — the router
        decides what to do with the failure.
        """
        settings = await self._snapshot()
        engine_id = str(settings.get(TRANSCRIPTION_ENGINE_KEY) or "")
        stored_options = settings.get(TRANSCRIPTION_OPTIONS_KEY)
        if not isinstance(stored_options, dict):
            stored_options = {}
        # The FULL per-engine map is returned, not just this id's bag. An empty
        # id means "use the deployment default", and only the router knows which
        # engine that is — resolving options here against an empty id silently
        # discarded the operator's tuning for the engine that then ran.
        return engine_id, dict(stored_options)

    async def llm_routing_selection(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Live read for the scoring pipeline: the primary target and its fallbacks.

        "Live" still means what it meant: a model switched in the settings screen
        applies to the next assessment in every process, including clip children
        and the Hatchet worker, without a restart. The snapshot behind it is
        evicted by the write itself in this process and by the table's change
        announcement in every other one — so the guarantee is now enforced by the
        database rather than re-established by querying it on every run.

        A read failure is *not* swallowed here. Unlike the preprocess toggle,
        silently scoring with a different model than the operator selected
        changes the marks a student receives; the caller decides what to do.
        """
        settings = await self._snapshot()
        primary = settings.get(LLM_PRIMARY_KEY)
        fallbacks = settings.get(LLM_FALLBACKS_KEY)
        if not isinstance(primary, dict):
            primary = {}
        if not isinstance(fallbacks, list):
            fallbacks = []
        return dict(primary), [dict(item) for item in fallbacks if isinstance(item, dict)]

    async def marking_selection(self) -> tuple[str, dict[str, Any]]:
        """Live read for the scoring pipeline: the marking mode and the raw
        panel configuration.

        Same freshness contract as ``llm_routing_selection`` — the snapshot is
        evicted by this process's own writes and by the table's change
        announcement in every other one — and the same refusal to swallow a
        read failure: silently marking with one model when the operator chose
        a panel changes what a student's sheet means.
        """
        settings = await self._snapshot()
        mode = str(settings.get(LLM_MARKING_MODE_KEY) or "single")
        panel = settings.get(LLM_PANEL_KEY)
        if not isinstance(panel, dict):
            panel = {}
        return mode, dict(panel)

    async def llm_preprocess_enabled(self) -> bool:
        """Live read for the pipeline. A read failure means 'off' — a settings
        lookup must never fail a scoring run."""
        try:
            return bool((await self._snapshot()).get(LLM_PREPROCESS_KEY))
        except Exception:
            logger.exception("Failed to read %s setting; treating as disabled.", LLM_PREPROCESS_KEY)
            return False
