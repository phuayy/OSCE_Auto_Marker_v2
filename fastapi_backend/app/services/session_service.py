from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.json_utils import extract_json_object
from app.core.utils import normalize_session_name, session_name_key
from app.domain.constants import SESSION_NAME_ADJECTIVES, SESSION_NAME_NOUNS
from app.repositories.session_repository import SessionEntry, SessionRepository


class SessionService:
    def __init__(self, settings: Settings, repository: SessionRepository) -> None:
        self.settings = settings
        self.repository = repository

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
        ordered = sorted(
            entries,
            key=lambda entry: self._parse_date(entry.session.get("createdAt")),
            reverse=True,
        )
        for entry in ordered:
            current_name = normalize_session_name(entry.session.get("name"))[: self.settings.session_name_max_length]
            key = session_name_key(current_name)
            if current_name and key not in used_keys:
                used_keys.add(key)
                entry.session["name"] = current_name
                continue
            entry.session["name"] = self.reserve_unique_session_name(used_keys)
            changed.append(entry)

        await asyncio.gather(*(self.repository.write_entry(entry) for entry in changed))
        return ordered, used_keys

    async def ensure_session_name(self, session_id: str, session: dict[str, Any]) -> dict[str, Any]:
        if session.get("name"):
            return session
        entries, _used = await self.ensure_names_for_index(await self.repository.read_all())
        for entry in entries:
            if str(entry.session.get("id")) == str(session_id):
                return entry.session
        return session

    async def list_sessions(self) -> list[dict[str, Any]]:
        entries, _used = await self.ensure_names_for_index(await self.repository.read_all())
        sessions: list[dict[str, Any]] = []
        for session in (entry.session for entry in entries):
            outputs = session.get("outputs") or {}
            pipeline = session.get("pipeline") if isinstance(session.get("pipeline"), dict) else {}
            sessions.append(
                {
                    "id": session.get("id"),
                    "name": session.get("name") or None,
                    "createdAt": session.get("createdAt") or None,
                    "status": session.get("status") or None,
                    "workflow": session.get("workflow") or None,
                    "segmentation": session.get("segmentation") or None,
                    "parentSessionId": session.get("parentSessionId") or None,
                    "clipSource": session.get("clipSource") or None,
                    "hasVideoClips": bool(outputs.get("videoClips")),
                    # Lightweight progress for the session-list cards: the UI
                    # gauges an in-flight session from its card instead of
                    # opening it (in-flight sessions are not enterable).
                    "currentStep": pipeline.get("currentStep") or None,
                    "pipelineStartedAt": pipeline.get("startedAt") or None,
                }
            )
        return sessions

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
            "pipeline": session.get("pipeline"),
            "parentSessionId": session.get("parentSessionId"),
            "clipSource": session.get("clipSource"),
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
                        "fileName": clip.get("fileName"),
                        "url": clip.get("url"),
                        "sizeBytes": int(clip.get("sizeBytes") or 0),
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
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse_date(value: Any) -> float:
        try:
            text = str(value or "").replace("Z", "+00:00")
            return datetime.fromisoformat(text).timestamp()
        except Exception:
            return 0.0
