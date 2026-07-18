from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.database.models import AppSettingRecord, utc_now
from app.database.orm import OrmDatabase


logger = logging.getLogger(__name__)

LLM_PREPROCESS_KEY = "llmTranscriptPreprocess"

# Known settings and their defaults. GET merges stored rows over these so the
# API response shape stays stable as settings are added.
DEFAULT_SETTINGS: dict[str, Any] = {
    LLM_PREPROCESS_KEY: False,
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
