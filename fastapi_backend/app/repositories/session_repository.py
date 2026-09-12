from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.exceptions import SessionWriteContractError, StaleSessionError
from app.core.json_utils import read_json_file
from app.database.models import SessionRecord, utc_now
from app.database.orm import OrmDatabase


logger = logging.getLogger(__name__)

# Sentinel key stamped onto every session dict on read, holding the row's
# ``updated_at`` at load time. ``write`` uses it as an optimistic-concurrency
# token: a mismatch is refused, not logged over. It is stripped before
# persistence.
LOADED_VERSION_KEY = "_loadedUpdatedAt"

# Keys that map to dedicated columns (or are transient) and therefore must not
# be duplicated into the flexible ``payload`` JSON column.
_NON_PAYLOAD_KEYS = ("id", "name", "status", "parentSessionId", "clipSource", "createdAt", LOADED_VERSION_KEY)


@dataclass
class SessionEntry:
    session: dict[str, Any]
    absolute_path: Path | None = None


def _version_token(updated_at: datetime | None) -> str | None:
    # Normalise to naive UTC so an in-memory ``utc_now()`` (tz-aware) and a value
    # round-tripped through the DB (naive on SQLite) yield the SAME token —
    # otherwise every write would look like a version mismatch.
    if updated_at is None:
        return None
    dt = updated_at
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat(timespec="microseconds")


def _optional_int(value: Any) -> int | None:
    """Integer form of :func:`_optional_float`, for JSON-extracted counters."""
    number = _optional_float(value)
    return None if number is None else int(number)


def _optional_float(value: Any) -> float | None:
    """Coerce a JSON-extracted numeric to float, tolerating backend quirks.

    ``as_float`` returns a real number on PostgreSQL, but SQLite hands back the
    raw JSON scalar, which is a string for some driver/version combinations —
    and a NULL either way when the key is absent.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _record_to_dict(record: SessionRecord) -> dict[str, Any]:
    result = dict(record.payload or {})
    result["id"] = record.id
    result["name"] = record.name
    result["status"] = record.status
    result["parentSessionId"] = record.parent_session_id
    result["clipSource"] = record.clip_source
    result[LOADED_VERSION_KEY] = _version_token(record.updated_at)
    if record.created_at is not None:
        dt = record.created_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        result["createdAt"] = dt.isoformat().replace("+00:00", "Z")
    return result


def _parse_created_at(session: dict[str, Any]) -> datetime:
    raw = str(session.get("createdAt") or "").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return utc_now()


class SessionRepository:
    def __init__(self, database: OrmDatabase, legacy_sessions_dir: Path | None = None) -> None:
        self.database = database
        self.legacy_sessions_dir = legacy_sessions_dir

    async def read(self, session_id: str) -> dict[str, Any]:
        async with self.database.session() as db_session:
            record = await db_session.get(SessionRecord, session_id)
            if record is None:
                raise FileNotFoundError(f"Session not found: {session_id}")
            return _record_to_dict(record)

    async def write(self, session: dict[str, Any]) -> None:
        """Persist ``session`` whole, under the write contract.

        * A row that does not exist yet is created from any dict — that is how
          uploads and clip children are born.
        * A row that exists is replaced only by a dict that was ``read`` from it
          (it carries ``_loadedUpdatedAt``) **and** whose stamp still matches
          the row. A mismatch means another writer committed in between, and
          the caller's document would erase that change; it raises
          :class:`StaleSessionError` so the caller can re-read and re-apply
          (see :meth:`SessionService.update`).
        * A dict with no stamp aimed at an existing row is refused outright:
          it was built by hand or is a projection, and storing it would replace
          the payload with whatever keys it happens to carry.
        """
        session_id = str(session["id"])
        now = utc_now()
        loaded_version = session.get(LOADED_VERSION_KEY)
        payload = {k: v for k, v in session.items() if k not in _NON_PAYLOAD_KEYS}
        async with self.database.transaction() as db_session:
            existing = await db_session.get(SessionRecord, session_id)
            if existing is not None:
                if loaded_version is None:
                    raise SessionWriteContractError(session_id)
                current_version = _version_token(existing.updated_at)
                if current_version is not None and current_version != loaded_version:
                    raise StaleSessionError(
                        session_id,
                        loaded_version=str(loaded_version),
                        current_version=current_version,
                    )
            if existing is None:
                record = SessionRecord(
                    id=session_id,
                    name=session.get("name"),
                    status=str(session.get("status") or "uploaded"),
                    parent_session_id=session.get("parentSessionId"),
                    clip_source=session.get("clipSource"),
                    payload=payload,
                    created_at=_parse_created_at(session),
                    updated_at=now,
                )
                db_session.add(record)
            else:
                existing.name = session.get("name")
                existing.status = str(session.get("status") or existing.status)
                existing.parent_session_id = session.get("parentSessionId")
                existing.clip_source = session.get("clipSource")
                existing.payload = payload
                existing.updated_at = now

        # Re-stamp the in-memory dict to the version just persisted, so the
        # read-once-write-many pattern (one caller marking several steps on one
        # object, with no other writer in between) keeps passing the check. A
        # genuinely stale writer holds a different dict whose stamp won't match.
        session[LOADED_VERSION_KEY] = _version_token(now)

    async def read_all(self) -> list[SessionEntry]:
        async with self.database.session() as db_session:
            result = await db_session.execute(
                select(SessionRecord).order_by(SessionRecord.created_at.desc())
            )
            return [SessionEntry(session=_record_to_dict(r)) for r in result.scalars().all()]

    async def read_index_projection(self) -> list[dict[str, Any]]:
        """Read only the fields the session-list cards render.

        ``read_all`` pulls every row's whole ``payload`` document — which embeds
        the complete content/communication/audio-professionalism score payloads —
        and deserialises all of it to produce ten shallow fields. This extracts
        those fields inside the database instead, so a list rebuild transfers
        bytes rather than megabytes.

        ``hasVideoClips`` is answered by probing the first clip's id rather than
        fetching the clip array: presence is all the caller needs.
        """
        payload = SessionRecord.payload
        statement = select(
            SessionRecord.id,
            SessionRecord.name,
            SessionRecord.status,
            SessionRecord.parent_session_id,
            SessionRecord.clip_source,
            SessionRecord.created_at,
            payload["workflow"].as_string().label("workflow"),
            payload["segmentation"].as_string().label("segmentation"),
            payload["pipeline", "currentStep"].as_string().label("current_step"),
            payload["pipeline", "stepProgress"].as_float().label("step_progress"),
            payload["pipeline", "startedAt"].as_string().label("pipeline_started_at"),
            payload["outputs", "videoClips", 0, "id"].as_string().label("first_clip_id"),
            payload["clipExport", "status"].as_string().label("clip_export_status"),
            payload["clipExport", "completed"].as_float().label("clip_export_completed"),
            payload["clipExport", "total"].as_float().label("clip_export_total"),
            payload["error"].as_string().label("error"),
        ).order_by(SessionRecord.created_at.desc())

        async with self.database.session() as db_session:
            rows = (await db_session.execute(statement)).all()

        projections: list[dict[str, Any]] = []
        for row in rows:
            created_at = row.created_at
            if created_at is not None and created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            projections.append(
                {
                    "id": row.id,
                    "name": row.name or None,
                    "createdAt": created_at.isoformat().replace("+00:00", "Z") if created_at else None,
                    "status": row.status or None,
                    "workflow": row.workflow or None,
                    "segmentation": row.segmentation or None,
                    "parentSessionId": row.parent_session_id or None,
                    "clipSource": row.clip_source or None,
                    "hasVideoClips": row.first_clip_id is not None,
                    "currentStep": row.current_step or None,
                    # Live completion of the current step (0-100), present only
                    # while a step that reports progress is running.
                    "stepProgress": _optional_float(row.step_progress),
                    "pipelineStartedAt": row.pipeline_started_at or None,
                    # Clip-export progress. Unlike the pipeline the export does
                    # not move session.status, so the list poll needs these to
                    # render its gauge.
                    "clipExportStatus": row.clip_export_status or None,
                    "clipExportCompleted": _optional_int(row.clip_export_completed),
                    "clipExportTotal": _optional_int(row.clip_export_total),
                    # Why a failed session failed. The card is the only place a
                    # user sees a session they cannot open, so the reason has
                    # to travel with the list, not just the full document.
                    "error": row.error or None,
                }
            )
        return projections

    async def list_child_ids(self, parent_session_id: str) -> list[str]:
        """Ids of every session whose parent is ``parent_session_id`` (clip
        assessment children of a long-video session)."""
        async with self.database.session() as db_session:
            result = await db_session.execute(
                select(SessionRecord.id).where(SessionRecord.parent_session_id == parent_session_id)
            )
            return [str(row) for row in result.scalars().all()]

    async def delete(self, session_id: str) -> bool:
        async with self.database.transaction() as db_session:
            record = await db_session.get(SessionRecord, session_id)
            if record is None:
                return False
            await db_session.delete(record)
        return True

    async def write_entry(self, entry: SessionEntry) -> None:
        await self.write(entry.session)

    async def migrate_legacy(self) -> int:
        if not self.legacy_sessions_dir or not self.legacy_sessions_dir.exists():
            return 0

        paths = list(self.legacy_sessions_dir.glob("*.json"))
        if not paths:
            return 0

        payloads: list[dict[str, Any]] = await asyncio.to_thread(self._load_legacy_files, paths)
        migrated = 0
        for payload in payloads:
            if not isinstance(payload, dict) or not payload.get("id"):
                continue
            session_id = str(payload["id"])
            async with self.database.session() as db_session:
                existing = await db_session.get(SessionRecord, session_id)
            if existing is not None:
                continue
            await self.write(payload)
            migrated += 1
        return migrated

    @staticmethod
    def _load_legacy_files(paths: list[Path]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for path in paths:
            payload = read_json_file(path)
            if isinstance(payload, dict):
                results.append(payload)
        return results
