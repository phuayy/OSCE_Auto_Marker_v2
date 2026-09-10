from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.json_utils import extract_json_object
from app.core.utils import normalize_session_name, session_name_key
from app.core.versioned_cache import VersionedCache
from app.domain.constants import SESSION_NAME_ADJECTIVES, SESSION_NAME_NOUNS
from app.repositories.session_repository import SessionEntry, SessionRepository
from app.services.change_feed_service import ChangeFeedService


SESSION_INDEX_CACHE_KEY = "session_index"


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
        await self.repository.write(session)

    async def read_all_entries(self) -> list[SessionEntry]:
        return await self.repository.read_all()

    async def ensure_names_for_index(self, entries: list[SessionEntry]) -> tuple[list[SessionEntry], set[str]]:
        used_keys: set[str] = set()
        changed: list[SessionEntry] = []
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
            entry.session["name"] = self.reserve_unique_session_name(used_keys, preferred)
            changed.append(entry)

        await asyncio.gather(*(self.repository.write_entry(entry) for entry in changed))
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
        raw = str(session.get("createdAt") or "").replace("Z", "+00:00")
        try:
            return f"Session {datetime.fromisoformat(raw).strftime('%Y-%m-%d %H:%M')}"
        except ValueError:
            return ""

    async def ensure_session_name(self, session_id: str, session: dict[str, Any]) -> dict[str, Any]:
        if session.get("name"):
            return session
        entries, _used = await self.ensure_names_for_index(await self.repository.read_all())
        for entry in entries:
            if str(entry.session.get("id")) == str(session_id):
                return entry.session
        return session

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

        # Legacy rows without a (unique) name still need the naming pass, which
        # reads and rewrites full payloads. It repairs every row in one go, so
        # this branch stops being taken after the first call.
        await self.ensure_names_for_index(await self.repository.read_all())
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
        entries, _used = await self.ensure_names_for_index(await self.repository.read_all())
        target = next((entry for entry in entries if str(entry.session.get("id")) == str(session_id)), None)
        if target is None:
            raise FileNotFoundError("Session not found.")
        next_key = session_name_key(next_name_raw)
        if any(
            str(entry.session.get("id")) != str(session_id)
            and session_name_key(entry.session.get("name")) == next_key
            for entry in entries
        ):
            raise FileExistsError("Session name must be unique.")
        target.session["name"] = next_name_raw
        await self.repository.write_entry(target)
        return target.session

    def reserve_unique_session_name(self, used_keys: set[str], preferred_name: str = "") -> str:
        base_preferred = normalize_session_name(preferred_name)[: self.settings.session_name_max_length]
        if base_preferred:
            base_key = session_name_key(base_preferred)
            if base_key not in used_keys:
                used_keys.add(base_key)
                return base_preferred
            for index in range(2, 51):
                candidate = f"{base_preferred} {index}"[: self.settings.session_name_max_length]
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

        def output_meta(key: str, include_url: bool = True, include_payload: bool = False) -> dict[str, Any] | None:
            item = outputs.get(key)
            if not item:
                return None
            payload = {
                "fileName": item.get("fileName"),
                "sizeBytes": item.get("sizeBytes"),
            }
            if include_url:
                payload["url"] = item.get("url")
            if include_payload:
                payload["payload"] = item.get("payload") or None
            return payload

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
            "pipeline": session.get("pipeline"),
            "parentSessionId": session.get("parentSessionId"),
            "clipSource": session.get("clipSource"),
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
                "audioProfessionalism": output_meta("audioProfessionalism", include_payload=True),
                "communicationScores": output_meta("communicationScores", include_payload=True),
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
                "scores": output_meta("scores", include_payload=True),
            },
            "error": session.get("error") or None,
        }

    async def read_output_payload(self, session: dict[str, Any], output_key: str) -> dict[str, Any]:
        output = (session.get("outputs") or {}).get(output_key) or {}
        absolute_path = output.get("absolutePath")
        if not absolute_path:
            raise FileNotFoundError(f"{output_key} output is not available yet.")
        path = Path(str(absolute_path))
        if not path.exists():
            raise FileNotFoundError(f"{output_key} output file not found.")
        return await asyncio.to_thread(lambda: extract_json_object(path.read_text(encoding="utf-8")))

    @staticmethod
    def _parse_date(value: Any) -> float:
        try:
            text = str(value or "").replace("Z", "+00:00")
            return datetime.fromisoformat(text).timestamp()
        except Exception:
            return 0.0
