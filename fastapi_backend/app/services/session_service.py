from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path
from typing import Any, Callable

from app.core.artifacts import read_artifact_payload
from app.core.config import Settings
from app.core.exceptions import StaleSessionError
from app.core.utils import normalize_session_name, parse_iso, session_name_key
from app.core.versioned_cache import VersionedCache
from app.core.session_cursor import decode_cursor, encode_cursor
from app.domain.actors import PROVENANCE_KEY, provenance_of
from app.domain.constants import SESSION_NAME_ADJECTIVES, SESSION_NAME_NOUNS
from app.repositories.session_repository import SessionEntry, SessionRepository
from app.services.change_feed_service import ChangeFeedService

logger = logging.getLogger(__name__)

SESSION_INDEX_CACHE_KEY = "session_index"

# A mutation applied to a freshly-read session document. Return ``False`` to
# say "nothing to write" (the document is returned as read); anything else
# commits. Must be synchronous and side-effect free apart from the document:
# it is re-run from scratch when another writer got in first.
SessionMutator = Callable[[dict[str, Any]], Any]

# How many times ``update`` re-reads and re-applies before giving up. Contention
# on one session is a handful of writers (pipeline, queue, the user), so a
# mutation that still cannot land after this many rounds is a bug, not load.
UPDATE_MAX_ATTEMPTS = 8


class SessionService:
    def __init__(
        self,
        settings: Settings,
        repository: SessionRepository,
        *,
        cache: VersionedCache | None = None,
        changes: ChangeFeedService | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        # Both optional so a bare SessionService (tests, scripts) still works —
        # it just rebuilds the index on every call, as it always did.
        self.cache = cache
        self.changes = changes

    async def migrate_legacy_sessions(self) -> int:
        return await self.repository.migrate_legacy()

    async def read(self, session_id: str) -> dict[str, Any]:
        return await self.repository.read(session_id)

    async def write(self, session: dict[str, Any]) -> None:
        """Persist a document that was either just created or ``read`` by this
        caller and not touched by anyone else since. For every other write —
        anything long-lived, anything racing a job — use :meth:`update`."""
        await self.repository.write(session)

    async def update(self, session_id: str, mutate: SessionMutator) -> dict[str, Any]:
        """Read → mutate → write, retried until the write lands on the version it read.

        This is the one way to change a session that another process may also be
        changing. The mutator sees the *current* document, so a rename landing
        while the pipeline marks a step keeps both: the pipeline's mutator is
        replayed on top of the renamed document instead of overwriting it with
        the copy it loaded an hour earlier.

        Returns the document as persisted (stamped), so a caller that keeps a
        working copy can adopt it and stay current.
        """
        last_error: StaleSessionError | None = None
        for attempt in range(1, UPDATE_MAX_ATTEMPTS + 1):
            session = await self.repository.read(session_id)
            if mutate(session) is False:
                return session
            try:
                await self.repository.write(session)
                return session
            except StaleSessionError as error:
                last_error = error
                logger.debug(
                    "Session %s changed under an update (attempt %d/%d); re-applying.",
                    session_id,
                    attempt,
                    UPDATE_MAX_ATTEMPTS,
                )
                # Yield so the writer that beat us can finish its own sequence
                # before we re-read; a hot loop here would just lose again.
                await asyncio.sleep(0)
        assert last_error is not None  # the loop only exits via return or here
        raise last_error

    async def read_all_entries(self) -> list[SessionEntry]:
        return await self.repository.read_all()

    async def list_child_ids(self, parent_session_id: str) -> list[str]:
        """Ids of the clip-assessment children of a long-video session."""
        return await self.repository.list_child_ids(parent_session_id)

    async def create_named(self, session: dict) -> None:
        preferred_name = str(session.get("name") or "")
        await self.repository.write(
            session, allocate_name=lambda used: self.reserve_unique_session_name(used, preferred_name),
        )

    async def ensure_names_for_index(self, entries: list[SessionEntry]) -> tuple[list[SessionEntry], set[str]]:
        used_keys: set[str] = set()
        reserved_keys = {session_name_key(entry.session.get("name")) for entry in entries}
        changed: list[tuple[SessionEntry, str | None]] = []
        # Walk oldest-first so an existing (historical) session always keeps its
        # name and a later duplicate is the one that gets suffixed — renaming
        # history out from under the user is far more surprising than adjusting
        # the newcomer.
        ordered = sorted(
            entries,
            key=lambda entry: self._parse_date(entry.session.get("createdAt")),
        )
        for entry in ordered:
            current_name = normalize_session_name(entry.session.get("name"))[: self.settings.session_name_max_length]
            key = session_name_key(current_name)
            if current_name and key not in used_keys:
                used_keys.add(key)
                entry.session["name"] = current_name
                continue
            # Duplicate or missing name: prefer a deterministic, meaningful base
            # (the colliding name itself, the uploaded video's file name, or the
            # creation date) so reserve_unique_session_name suffixes it instead
            # of inventing a random one.
            preferred = current_name or self._derive_fallback_name(entry.session)
            previous_name = entry.session.get("name")
            entry.session["name"] = self.reserve_unique_session_name(used_keys | reserved_keys, preferred)
            used_keys.add(session_name_key(entry.session["name"]))
            changed.append((entry, previous_name))

        await asyncio.gather(
            *(
                self.repository.update_name(str(entry.session["id"]), previous_name, str(entry.session["name"]))
                for entry, previous_name in changed
            )
        )
        ordered.reverse()  # callers (list_sessions, ensure_session_name) expect newest-first
        return ordered, used_keys

    @staticmethod
    def _derive_fallback_name(session: dict[str, Any]) -> str:
        """Deterministic display name for a session that has none: the uploaded
        video's original file name (without extension), else the creation date."""
        video = (session.get("files") or {}).get("video") or {}
        stem = Path(str(video.get("originalName") or "")).stem.strip()
        if stem:
            return stem
        created = parse_iso(session.get("createdAt"))
        return f"Session {created.strftime('%Y-%m-%d %H:%M')}" if created else ""

    async def ensure_session_name(self, session_id: str, session: dict[str, Any]) -> dict[str, Any]:
        if session.get("name"):
            return session
        await self.ensure_names_for_index(await self.repository.read_name_entries())
        return await self.read(session_id)

    async def list_page(
        self, *, limit: int = 200, cursor: str | None = None,
        parent_session_id: str | None = None, roots_only: bool = False,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 200:
            raise ValueError("Session limit must be between 1 and 200.")
        after = decode_cursor(cursor)
        if roots_only and parent_session_id is not None:
            raise ValueError("Root and child session filters cannot be combined.")
        query = dict(limit=limit + 1, after=after, parent_session_id=parent_session_id, roots_only=roots_only)

        async def build() -> dict[str, Any]:
            rows = await self.repository.read_index_projection(**query)
            if self._needs_name_backfill(rows):
                await self.ensure_names_for_index(await self.repository.read_name_entries())
                rows = await self.repository.read_index_projection(**query)
            has_more = len(rows) > limit
            page = rows[:limit]
            return {"sessions": page, "nextCursor": encode_cursor(page[-1]) if has_more else None}

        if self.cache is None or self.changes is None:
            return await build()
        token = await self.changes.token(("sessions",))
        return await self.cache.get_or_build(
            f"{SESSION_INDEX_CACHE_KEY}:{limit}:{after}:{parent_session_id}:{roots_only}", token, build, tables=("sessions",),
        )

    async def list_sessions(self) -> list[dict[str, Any]]:
        """The session-list projection, served from cache while nothing changed.

        This is the most-polled endpoint in the app, so it must not re-read every
        session payload on each call. The cache is keyed on the ``sessions``
        change counter, which a database trigger bumps — including for writes
        made by the Hatchet worker process — so a hit is always current.
        """
        if self.cache is None or self.changes is None:
            return await self._build_session_index()
        token = await self.changes.token(("sessions",))
        return await self.cache.get_or_build(
            SESSION_INDEX_CACHE_KEY,
            token,
            self._build_session_index,
            # Declaring the dependency lets a committed write to `sessions`
            # evict this entry the moment the database announces it, instead of
            # the staleness only being noticed on the next token comparison.
            tables=("sessions",),
        )

    async def _build_session_index(self) -> list[dict[str, Any]]:
        projections = await self.repository.read_index_projection()
        if not self._needs_name_backfill(projections):
            return projections

        await self.ensure_names_for_index(await self.repository.read_name_entries())
        return await self.repository.read_index_projection()

    @staticmethod
    def _needs_name_backfill(projections: list[dict[str, Any]]) -> bool:
        seen: set[str] = set()
        for projection in projections:
            key = session_name_key(projection.get("name"))
            if not key or key in seen:
                return True
            seen.add(key)
        return False

    async def rename_session(self, session_id: str, next_name: str) -> dict[str, Any]:
        next_name_raw = normalize_session_name(next_name)[: self.settings.session_name_max_length]
        if not next_name_raw:
            raise ValueError("Session name is required.")
        await self.repository.rename(str(session_id), next_name_raw)
        return await self.read(session_id)

    def reserve_unique_session_name(self, used_keys: set[str], preferred_name: str = "") -> str:
        base_preferred = normalize_session_name(preferred_name)[: self.settings.session_name_max_length]
        if base_preferred:
            base_key = session_name_key(base_preferred)
            if base_key not in used_keys:
                used_keys.add(base_key)
                return base_preferred
            for index in range(2, 51):
                suffix = f" {index}"
                candidate = f"{base_preferred[:self.settings.session_name_max_length - len(suffix)]}{suffix}"
                key = session_name_key(candidate)
                if key not in used_keys:
                    used_keys.add(key)
                    return candidate

        for _attempt in range(80):
            candidate = self.generate_random_session_name()[: self.settings.session_name_max_length]
            key = session_name_key(candidate)
            if key not in used_keys:
                used_keys.add(key)
                return candidate

        fallback_index = len(used_keys) + 1
        while session_name_key(f"Session {fallback_index}") in used_keys:
            fallback_index += 1
        fallback_name = f"Session {fallback_index}"[: self.settings.session_name_max_length]
        used_keys.add(session_name_key(fallback_name))
        return fallback_name

    @staticmethod
    def generate_random_session_name() -> str:
        return f"{random.choice(SESSION_NAME_ADJECTIVES)} {random.choice(SESSION_NAME_NOUNS)} {random.randint(100, 999)}"

    @staticmethod
    def public_session(session: dict[str, Any]) -> dict[str, Any]:
        files = session.get("files") or {}
        outputs = session.get("outputs") or {}
        video = files.get("video") or {}
        case_study = files.get("caseStudy")

        def public_artifact(item: Any, include_url: bool = True) -> dict[str, Any] | None:
            """One stored artefact record as the browser may see it: name, size
            and the /media URL. Never ``absolutePath`` — server paths stay behind
            ``read_artifact_payload``."""
            if not isinstance(item, dict) or not item:
                return None
            payload = {
                "fileName": item.get("fileName"),
                "sizeBytes": item.get("sizeBytes"),
            }
            if include_url:
                payload["url"] = item.get("url")
            return payload

        def output_meta(key: str, include_url: bool = True) -> dict[str, Any] | None:
            return public_artifact(outputs.get(key), include_url)

        def public_panel_artifacts(scores: Any) -> dict[str, Any] | None:
            """Where a panel run's marker sheets and adjudication record are
            served from (``PanelMarking.run`` attaches them to the scores
            output), projected artefact by artefact so no server path leaks.
            ``None`` for a single-model sheet, so the key is always present."""
            panel = scores.get("panelArtifacts") if isinstance(scores, dict) else None
            if not isinstance(panel, dict):
                return None
            markers = panel.get("markers")
            return {
                "markers": {
                    str(key): public_artifact(item)
                    for key, item in (markers.items() if isinstance(markers, dict) else ())
                    if isinstance(item, dict)
                },
                # None on a degraded run: only a record this run wrote is linked.
                "adjudication": public_artifact(panel.get("adjudication")),
            }

        def public_storage_ref(item: dict[str, Any]) -> dict[str, Any] | None:
            storage_ref = item.get("storageRef")
            if not isinstance(storage_ref, dict):
                return None
            provider = storage_ref.get("provider")
            uri = storage_ref.get("uri")
            if provider == "local" and str(uri or "").startswith("file://"):
                uri = None
            return {
                "provider": provider,
                "bucket": storage_ref.get("bucket"),
                "key": storage_ref.get("key"),
                "uri": uri,
                "sizeBytes": storage_ref.get("sizeBytes"),
                "mimeType": storage_ref.get("mimeType"),
                "checksumSha256": storage_ref.get("checksumSha256"),
                "etag": storage_ref.get("etag"),
                "generation": storage_ref.get("generation"),
                "status": storage_ref.get("status"),
                "committedAt": storage_ref.get("committedAt"),
            }

        scores = output_meta("scores")
        if scores is not None:
            # A panel run's per-marker sheets and the adjudication record. Present
            # on every scores output (None for a single-model sheet) so the score
            # tab reads one shape. New payload fields must be whitelisted here or
            # the browser never sees them (see the videoClips "kind" lesson).
            scores["panelArtifacts"] = public_panel_artifacts(outputs.get("scores"))

        clips = outputs.get("videoClips")
        return {
            "id": session.get("id"),
            "name": session.get("name") or None,
            "createdAt": session.get("createdAt"),
            "status": session.get("status"),
            "workflow": session.get("workflow"),
            "segmentation": session.get("segmentation") or None,
            # Occupancy rule the person detector ran (or will run) with. New
            # payload fields must be whitelisted here or the browser never sees
            # them (see the videoClips "kind" lesson).
            "segmentationOptions": session.get("segmentationOptions") or None,
            # Region-of-interest the person detector ran (or will run) with.
            # New payload fields must be whitelisted here or the browser
            # never sees them (see the videoClips "kind" lesson).
            "regionFocusOptions": session.get("regionFocusOptions") or None,
            "pipeline": session.get("pipeline"),
            "parentSessionId": session.get("parentSessionId"),
            "clipSource": session.get("clipSource"),
            # Who created it, as they were at the time; None for a session
            # recorded before creators were.
            PROVENANCE_KEY: provenance_of(session),
            # Progress of the durable clip-export job, or None when this session
            # has never been split. Drives the timeline editor's export gauge.
            "clipExport": session.get("clipExport") or None,
            # Which engine produced this session's transcript, and whether it
            # labelled speakers. Recorded per run because the engine is
            # operator-selectable, so two sessions in one list may differ.
            "transcription": session.get("transcription") or None,
            # Transcription-corpus snapshot chosen at upload (or None). New
            # payload fields must be whitelisted here or the browser never sees
            # them (see the videoClips "kind" lesson).
            "corpus": {
                "id": (session.get("corpus") or {}).get("id"),
                "name": (session.get("corpus") or {}).get("name"),
                "terms": (session.get("corpus") or {}).get("terms") or [],
            }
            if session.get("corpus")
            else None,
            "files": {
                "video": {
                    "originalName": video.get("originalName"),
                    "fileName": video.get("fileName"),
                    "url": video.get("url"),
                    "sizeBytes": video.get("sizeBytes"),
                    "mimeType": video.get("mimeType"),
                    "uploadStatus": video.get("uploadStatus"),
                    "uploadedBytes": video.get("uploadedBytes"),
                    "storageRef": public_storage_ref(video),
                },
                "caseStudy": {
                    "originalName": case_study.get("originalName"),
                    "fileName": case_study.get("fileName"),
                    "sizeBytes": case_study.get("sizeBytes"),
                    "mimeType": case_study.get("mimeType"),
                    "uploadStatus": case_study.get("uploadStatus"),
                    "uploadedBytes": case_study.get("uploadedBytes"),
                    "rubricAssetId": case_study.get("rubricAssetId"),
                    "rubricDeduplicated": case_study.get("rubricDeduplicated"),
                    "contentSha256": case_study.get("contentSha256"),
                    "storageRef": public_storage_ref(case_study),
                }
                if case_study
                else None,
            },
            "outputs": {
                "audio": output_meta("audio"),
                "transcript": output_meta("transcript"),
                "whisperxJson": output_meta("whisperxJson", include_url=False),
                "subtitle": output_meta("subtitle"),
                "subtitleTrack": output_meta("subtitleTrack"),
                "audioProfessionalism": output_meta("audioProfessionalism"),
                "communicationScores": output_meta("communicationScores"),
                "videoClips": [
                    {
                        "id": clip.get("id"),
                        "label": str(clip.get("label") or ""),
                        "start": float(clip.get("start") or 0),
                        "end": float(clip.get("end") or 0),
                        # session | intermission — drives the greyed manual-crop
                        # segments and keeps intermissions out of export/assessment.
                        "kind": str(clip.get("kind") or "session"),
                        "personCount": clip.get("personCount"),
                        "fileName": clip.get("fileName"),
                        "url": clip.get("url"),
                        "sizeBytes": int(clip.get("sizeBytes") or 0),
                        # A draft has a range but no MP4 yet: the export job has
                        # not reached it. Assessment is refused until it has.
                        "isDraft": bool(clip.get("isDraft")),
                    }
                    for clip in clips
                ]
                if isinstance(clips, list)
                else None,
                "scores": scores,
            },
            "error": session.get("error") or None,
        }

    async def read_output_payload(self, session: dict[str, Any], output_key: str) -> dict[str, Any]:
        output = (session.get("outputs") or {}).get(output_key) or {}
        return await read_artifact_payload(output)

    @staticmethod
    def _parse_date(value: Any) -> float:
        parsed = parse_iso(value)
        return parsed.timestamp() if parsed else 0.0
