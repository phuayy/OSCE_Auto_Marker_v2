from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import SessionWriteContractError, StaleSessionError
from app.core.json_utils import read_json_file
from app.core.utils import parse_iso, session_name_key
from app.database.models import SessionRecord, utc_now
from app.database.orm import OrmDatabase
from app.domain.actors import PROVENANCE_KEY, provenance_of, provenance_user_id

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


def _compact_steps(value: Any) -> dict[str, Any] | None:
    """Status + live progress for every pipeline step, for the list projection.

    Under ``PARALLEL_SCORING`` more than one step can be ``running`` at once
    (the content and communication branches), so the card needs every step's
    own reading — not just the single ``currentStep``/``stepProgress`` pair —
    to gauge them independently. Metadata, error text and timestamps are
    dropped: the cards have no use for them, and shipping them would turn a
    lightweight list poll back into the megabyte-per-row read this projection
    exists to avoid.

    ``None`` when the session has not recorded a ``pipeline.steps`` object at
    all (a fresh or pre-migration row); ``{}`` when it has one but it is
    empty. Progress is coerced with :func:`_optional_float` for the same
    SQLite-hands-back-a-string reason ``stepProgress`` already needs it.
    """
    if not isinstance(value, dict):
        return None
    compact: dict[str, Any] = {}
    for name, state in value.items():
        if not isinstance(state, dict):
            continue
        compact[str(name)] = {
            "status": state.get("status"),
            "progress": _optional_float(state.get("progress")),
        }
    return compact


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
    return parse_iso(session.get("createdAt")) or utc_now()


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

    async def write(self, session: dict[str, Any], *, allocate_name: Callable[[set[str]], str] | None = None) -> None:
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
            if allocate_name is not None:
                await self._lock_names(db_session)
                names = await db_session.execute(select(SessionRecord.name))
                session["name"] = allocate_name({session_name_key(name) for name in names.scalars() if name})
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
                    created_by=provenance_user_id(session),
                    payload=payload,
                    created_at=_parse_created_at(session),
                    updated_at=now,
                )
                db_session.add(record)
            else:
                result = await db_session.execute(
                    update(SessionRecord)
                    .where(SessionRecord.id == session_id, SessionRecord.updated_at == existing.updated_at)
                    .values(
                        name=session.get("name"),
                        status=str(session.get("status") or existing.status),
                        parent_session_id=session.get("parentSessionId"),
                        clip_source=session.get("clipSource"),
                        created_by=provenance_user_id(session),
                        payload=payload,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise StaleSessionError(
                        session_id, loaded_version=str(loaded_version), current_version="concurrent write",
                    )

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

    async def name_keys(self, *, exclude_id: str | None = None) -> set[str]:
        statement = select(SessionRecord.name)
        if exclude_id is not None:
            statement = statement.where(SessionRecord.id != exclude_id)
        async with self.database.session() as db_session:
            result = await db_session.execute(statement)
            return {session_name_key(name) for name in result.scalars() if name}

    async def _lock_names(self, db_session: AsyncSession) -> None:
        if self.database.engine.dialect.name == "sqlite":
            await db_session.execute(text("BEGIN IMMEDIATE"))
        else:
            await db_session.execute(text("SELECT pg_advisory_xact_lock(7482673901)"))

    async def rename(self, session_id: str, name: str) -> None:
        async with self.database.transaction() as db_session:
            await self._lock_names(db_session)
            names = await db_session.execute(
                select(SessionRecord.name).where(SessionRecord.id != session_id)
            )
            if session_name_key(name) in {session_name_key(value) for value in names.scalars() if value}:
                raise FileExistsError("Session name must be unique.")
            result = await db_session.execute(
                update(SessionRecord).where(SessionRecord.id == session_id).values(name=name, updated_at=utc_now())
            )
            if result.rowcount != 1:
                raise FileNotFoundError("Session not found.")

    async def read_name_entries(self) -> list[SessionEntry]:
        async with self.database.session() as db_session:
            rows = await db_session.execute(select(
                SessionRecord.id, SessionRecord.name, SessionRecord.created_at,
                SessionRecord.payload["files", "video", "originalName"].as_string().label("video_name"),
            ))
            return [
                SessionEntry(session={
                    "id": row.id, "name": row.name, "createdAt": row.created_at.isoformat(),
                    "files": {"video": {"originalName": row.video_name}},
                })
                for row in rows
            ]

    async def update_name(self, session_id: str, expected: str | None, name: str) -> bool:
        async with self.database.transaction() as db_session:
            await self._lock_names(db_session)
            names = await db_session.execute(select(SessionRecord.name).where(SessionRecord.id != session_id))
            if session_name_key(name) in {session_name_key(value) for value in names.scalars() if value}:
                return False
            result = await db_session.execute(
                update(SessionRecord)
                .where(SessionRecord.id == session_id, SessionRecord.name == expected)
                .values(name=name, updated_at=utc_now())
            )
            return result.rowcount == 1

    async def child_summaries(self, parent_session_id: str) -> list[SessionEntry]:
        payload = SessionRecord.payload
        async with self.database.session() as db_session:
            rows = await db_session.execute(
                select(
                    SessionRecord.id, SessionRecord.name, SessionRecord.status, SessionRecord.clip_source,
                    payload["outputs", "scores", "absolutePath"].as_string().label("scores_path"),
                    payload["outputs", "communicationScores", "absolutePath"].as_string().label("communication_path"),
                ).where(SessionRecord.parent_session_id == parent_session_id)
            )
            return [
                SessionEntry(session={
                    "id": row.id, "name": row.name, "status": row.status, "clipSource": row.clip_source,
                    "outputs": {
                        "scores": {"absolutePath": row.scores_path},
                        "communicationScores": {"absolutePath": row.communication_path},
                    },
                })
                for row in rows
            ]

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
            payload["pipeline", "steps"].as_json().label("steps"),
            payload["pipeline", "startedAt"].as_string().label("pipeline_started_at"),
            payload["outputs", "videoClips", 0, "id"].as_string().label("first_clip_id"),
            payload["clipExport", "status"].as_string().label("clip_export_status"),
            payload["clipExport", "completed"].as_float().label("clip_export_completed"),
            payload["clipExport", "total"].as_float().label("clip_export_total"),
            payload["error"].as_string().label("error"),
            payload[PROVENANCE_KEY].as_json().label("created_by"),
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
                    # Per-step status + progress, so a parallel branch's own
                    # reading survives even while it is not `currentStep`.
                    "steps": _compact_steps(row.steps),
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
                    # Who created the session, as they were at the time (the
                    # card says "by Dr M"); None for a row that predates this.
                    "createdBy": provenance_of({PROVENANCE_KEY: row.created_by}),
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

    async def find_clip_children(self, parent_session_id: str, clip_id: str) -> list[dict[str, Any]]:
        """Assessment children of one *clip*, newest first.

        A clip is meant to have exactly one child session, and this is what
        makes "find or create" answerable without loading every child's whole
        payload: the parent is an indexed column and the clip id is extracted
        from ``clip_source`` inside the database. More than one row here means
        an older build created duplicates; newest-first ordering makes the most
        recent one the one the UI and the re-run path act on.
        """
        async with self.database.session() as db_session:
            rows = (
                await db_session.execute(
                    select(
                        SessionRecord.id,
                        SessionRecord.status,
                        SessionRecord.created_at,
                        SessionRecord.clip_source,
                    )
                    .where(
                        SessionRecord.parent_session_id == parent_session_id,
                        SessionRecord.clip_source["clipId"].as_string() == str(clip_id),
                    )
                    .order_by(SessionRecord.created_at.desc())
                )
            ).all()
        children: list[dict[str, Any]] = []
        for row in rows:
            created_at = row.created_at
            if created_at is not None and created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            children.append(
                {
                    "id": row.id,
                    "status": row.status or None,
                    "createdAt": created_at.isoformat().replace("+00:00", "Z") if created_at else None,
                    "clipSource": row.clip_source or None,
                }
            )
        return children

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
