from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.database.models import AppSettingRecord, utc_now
from app.database.orm import OrmDatabase


logger = logging.getLogger(__name__)

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
}


class AppSettingsRepository:
    def __init__(self, database: OrmDatabase) -> None:
        self.database = database

    async def get_all(self) -> dict[str, Any]:
        async with self.database.session() as db:
            records = (await db.scalars(select(AppSettingRecord))).all()
        stored = {record.key: record.value for record in records}
        return {**DEFAULT_SETTINGS, **stored}

    async def set_values(self, values: dict[str, Any]) -> dict[str, Any]:
        async with self.database.transaction() as db:
            for key, value in values.items():
                record = await db.get(AppSettingRecord, key)
                if record is None:
                    db.add(AppSettingRecord(key=key, value=value))
                else:
                    record.value = value
                    record.updated_at = utc_now()
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
        async with self.database.session() as db:
            engine_record = await db.get(AppSettingRecord, TRANSCRIPTION_ENGINE_KEY)
            options_record = await db.get(AppSettingRecord, TRANSCRIPTION_OPTIONS_KEY)
        engine_id = str((engine_record.value if engine_record is not None else "") or "")
        stored_options = options_record.value if options_record is not None else {}
        if not isinstance(stored_options, dict):
            stored_options = {}
        # The FULL per-engine map is returned, not just this id's bag. An empty
        # id means "use the deployment default", and only the router knows which
        # engine that is — resolving options here against an empty id silently
        # discarded the operator's tuning for the engine that then ran.
        return engine_id, stored_options

    async def llm_routing_selection(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Live read for the scoring pipeline: the primary target and its fallbacks.

        Read on every run rather than cached, so switching model in the settings
        screen takes effect on the next assessment in every process — including
        clip children and the Hatchet worker — without a restart.

        A read failure is *not* swallowed here. Unlike the preprocess toggle,
        silently scoring with a different model than the operator selected
        changes the marks a student receives; the caller decides what to do.
        """
        async with self.database.session() as db:
            primary_record = await db.get(AppSettingRecord, LLM_PRIMARY_KEY)
            fallbacks_record = await db.get(AppSettingRecord, LLM_FALLBACKS_KEY)
        primary = primary_record.value if primary_record is not None else {}
        fallbacks = fallbacks_record.value if fallbacks_record is not None else []
        if not isinstance(primary, dict):
            primary = {}
        if not isinstance(fallbacks, list):
            fallbacks = []
        return primary, [item for item in fallbacks if isinstance(item, dict)]

    async def llm_preprocess_enabled(self) -> bool:
        """Live read for the pipeline. A read failure means 'off' — a settings
        lookup must never fail a scoring run."""
        try:
            async with self.database.session() as db:
                record = await db.get(AppSettingRecord, LLM_PREPROCESS_KEY)
            return bool(record.value) if record is not None else bool(DEFAULT_SETTINGS[LLM_PREPROCESS_KEY])
        except Exception:
            logger.exception("Failed to read %s setting; treating as disabled.", LLM_PREPROCESS_KEY)
            return False
