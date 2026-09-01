from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.json_utils import read_json_file
from app.database.models import SessionRecord, utc_now
from app.database.orm import OrmDatabase


logger = logging.getLogger(__name__)

# Sentinel key stamped onto every session dict on read, holding the row's
# ``updated_at`` at load time. ``write`` uses it as an optimistic-concurrency
# token to detect (and log) lost updates. It is stripped before persistence.
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
        session_id = str(session["id"])
        now = utc_now()
        loaded_version = session.get(LOADED_VERSION_KEY)
        payload = {k: v for k, v in session.items() if k not in _NON_PAYLOAD_KEYS}
        async with self.database.transaction() as db_session:
            existing = await db_session.get(SessionRecord, session_id)
            if existing is not None and loaded_version is not None:
                current_version = _version_token(existing.updated_at)
                if current_version is not None and current_version != loaded_version:
                    # The row changed between this caller's read and write — a
                    # concurrent writer's update is about to be overwritten
                    # (last-writer-wins). Surface it so the race is observable.
                    logger.warning(
                        "Concurrent session modification detected; overwriting newer state.",
                        extra={
                            "trace_id": session_id,
                            "stage": "session_write",
                            "loaded_version": loaded_version,
                            "current_version": current_version,
                        },
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

        # Re-stamp the in-memory dict to the version we just persisted, so the
        # common read-once-write-many pattern (the pipeline marks many steps on a
        # single session object) is NOT flagged as a concurrent modification. A
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
