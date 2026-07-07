from __future__ import annotations

from datetime import timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.database.models import VideoRecord
from app.database.orm import OrmDatabase


class VideoRepository:
    def __init__(self, db: OrmDatabase) -> None:
        self.db = db

    async def save(
        self,
        session_id: str,
        storage_ref: dict[str, Any],
        *,
        original_name: str,
        safe_name: str,
    ) -> dict[str, Any]:
        async with self.db.session() as s:
            result = await s.execute(
                select(VideoRecord).where(VideoRecord.session_id == session_id)
            )
            rec = result.scalar_one_or_none()
            if rec is None:
                rec = VideoRecord(id=str(uuid4()), session_id=session_id)
                s.add(rec)
            rec.original_name = original_name
            rec.safe_name = safe_name
            rec.size_bytes = storage_ref.get("sizeBytes")
            rec.mime_type = storage_ref.get("mimeType")
            rec.storage_provider = str(storage_ref.get("provider") or "local")
            rec.storage_key = storage_ref.get("key")
            rec.absolute_path = storage_ref.get("localPath")
            rec.public_url = storage_ref.get("publicUrl")
            rec.storage_ref_json = dict(storage_ref)
            await s.commit()
            await s.refresh(rec)
        return self._to_dict(rec)

    async def get_for_session(self, session_id: str) -> dict[str, Any] | None:
        async with self.db.session() as s:
            result = await s.execute(
                select(VideoRecord).where(VideoRecord.session_id == session_id)
            )
            rec = result.scalar_one_or_none()
            return self._to_dict(rec) if rec else None

    @staticmethod
    def _to_dict(rec: VideoRecord) -> dict[str, Any]:
        created = rec.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return {
            "id": rec.id,
            "sessionId": rec.session_id,
            "originalName": rec.original_name,
            "safeName": rec.safe_name,
            "sizeBytes": rec.size_bytes,
            "mimeType": rec.mime_type,
            "storageProvider": rec.storage_provider,
            "storageKey": rec.storage_key,
            "absolutePath": rec.absolute_path,
            "publicUrl": rec.public_url,
            "storageRef": rec.storage_ref_json,
            "createdAt": created.isoformat().replace("+00:00", "Z") if created else None,
        }
