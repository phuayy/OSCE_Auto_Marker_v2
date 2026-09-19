from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.database.models import PromptVersionRecord
from app.database.orm import OrmDatabase


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PromptCatalogEntry:
    """One prompt's static wording, as scripts/prompt_catalog.py emits it."""

    key: str
    version: str
    text: str
    source_script: str


@dataclass(frozen=True)
class PromptVersionSyncResult:
    inserted: list[str]
    unchanged: list[str]
    drifted: list[str]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record_to_dict(record: PromptVersionRecord, *, include_text: bool) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": record.id,
        "promptKey": record.prompt_key,
        "version": record.version,
        "contentHash": record.content_sha256,
        "sourceScript": record.source_script,
        "recordedAt": record.recorded_at.isoformat() if record.recorded_at else None,
    }
    if include_text:
        data["templateText"] = record.template_text
    return data


class PromptVersionRepository:
    def __init__(self, database: OrmDatabase) -> None:
        self.database = database

    async def record_if_new(self, entries: list[PromptCatalogEntry]) -> PromptVersionSyncResult:
        """Insert any (prompt_key, version) pair not already stored.

        A pair whose stored hash differs from what was just computed means the
        wording changed without the version string being bumped — the exact
        invariant a ``PROMPT_VERSION``-style constant exists to protect
        ("sheets across versions are not comparable"). That is logged loudly
        and the stored row is left untouched: an immutable ledger's whole
        value is that a recorded version's text never moves under it.
        """
        inserted: list[str] = []
        unchanged: list[str] = []
        drifted: list[str] = []
        async with self.database.transaction() as db:
            for entry in entries:
                computed_hash = content_hash(entry.text)
                existing = await db.scalar(
                    select(PromptVersionRecord).where(
                        PromptVersionRecord.prompt_key == entry.key,
                        PromptVersionRecord.version == entry.version,
                    )
                )
                label = f"{entry.key}@{entry.version}"
                if existing is None:
                    db.add(
                        PromptVersionRecord(
                            id=str(uuid4()),
                            prompt_key=entry.key,
                            version=entry.version,
                            content_sha256=computed_hash,
                            template_text=entry.text,
                            source_script=entry.source_script,
                        )
                    )
                    inserted.append(label)
                elif existing.content_sha256 == computed_hash:
                    unchanged.append(label)
                else:
                    logger.warning(
                        "Prompt '%s' version '%s' is already recorded with different wording "
                        "(stored hash %s, current hash %s). The version string was not bumped "
                        "when the wording changed; the stored snapshot is kept unchanged. Bump "
                        "the version constant in %s.",
                        entry.key,
                        entry.version,
                        existing.content_sha256[:12],
                        computed_hash[:12],
                        entry.source_script,
                    )
                    drifted.append(label)
        return PromptVersionSyncResult(inserted=inserted, unchanged=unchanged, drifted=drifted)

    async def list_versions(self, prompt_key: str | None = None) -> list[dict[str, Any]]:
        async with self.database.session() as db:
            statement = select(PromptVersionRecord).order_by(
                PromptVersionRecord.prompt_key, PromptVersionRecord.recorded_at.desc()
            )
            if prompt_key:
                statement = statement.where(PromptVersionRecord.prompt_key == prompt_key)
            records = (await db.scalars(statement)).all()
        return [_record_to_dict(record, include_text=False) for record in records]

    async def get(self, record_id: str) -> dict[str, Any] | None:
        async with self.database.session() as db:
            record = await db.get(PromptVersionRecord, record_id)
        return _record_to_dict(record, include_text=True) if record else None
