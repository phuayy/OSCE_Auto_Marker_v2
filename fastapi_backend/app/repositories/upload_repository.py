from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update

from app.core.exceptions import AppError
from app.core.json_utils import read_json_file
from app.core.utils import parse_iso
from app.database.models import SessionRecord, UploadRecord, utc_now
from app.database.orm import OrmDatabase
from app.domain.actors import provenance_user_id

logger = logging.getLogger(__name__)
_VERSION = "_uploadVersion"


def _payload(row: UploadRecord) -> dict[str, Any]:
    return {**deepcopy(row.payload_json), "id": row.id, "sessionId": row.session_id,
            "status": row.status, "files": deepcopy(row.files_json), _VERSION: row.updated_at.isoformat()}


class UploadRepository:
    def __init__(self, database: OrmDatabase, uploads_dir: Path | None = None) -> None:
        self.database = database
        self.uploads_dir = uploads_dir

    @asynccontextmanager
    async def locked(self, upload_id: str):
        async with self.database.unit_of_work() as db:
            row = await db.scalar(select(UploadRecord).where(UploadRecord.id == upload_id).with_for_update())
            if row is None:
                raise FileNotFoundError(f"Upload not found: {upload_id}")
            yield

    async def read(self, upload_id: str) -> dict[str, Any]:
        async with self.database.session() as db:
            row = await db.get(UploadRecord, upload_id)
            if row is None:
                raise FileNotFoundError(f"Upload not found: {upload_id}")
            return _payload(row)

    async def write(self, upload: dict[str, Any]) -> None:
        now = utc_now()
        expires = parse_iso(upload.get("expiresAt"))
        if expires is None:
            raise ValueError("An upload must have a valid expiry timestamp.")
        values = dict(
            session_id=str(upload["sessionId"]), status=str(upload["status"]),
            files_json=deepcopy(upload.get("files") or []), created_by=provenance_user_id(upload),
            payload_json={key: deepcopy(value) for key, value in upload.items()
                          if key not in {"id", "sessionId", "status", "files", _VERSION}},
            created_at=parse_iso(upload.get("createdAt")) or now, expires_at=expires, updated_at=now,
        )
        async with self.database.transaction() as db:
            if upload.get(_VERSION):
                result = await db.execute(update(UploadRecord).where(
                    UploadRecord.id == str(upload["id"]),
                    UploadRecord.updated_at == parse_iso(upload[_VERSION]),
                ).values(**values).execution_options(synchronize_session=False))
                if result.rowcount != 1:
                    raise AppError("Upload changed concurrently; retry the request.", status_code=409)
                db.expire_all()
            else:
                db.add(UploadRecord(id=str(upload["id"]), **values))
        upload[_VERSION] = now.isoformat()

    async def read_all(self, *, expired: bool = False) -> list[dict[str, Any]]:
        statement = select(UploadRecord)
        if expired:
            statement = statement.where(UploadRecord.expires_at < utc_now())
        async with self.database.session() as db:
            return [_payload(row) for row in await db.scalars(statement)]

    async def delete_expired(self, upload_id: str) -> None:
        async with self.database.transaction() as db:
            await db.execute(delete(UploadRecord).where(
                UploadRecord.id == upload_id, UploadRecord.expires_at < utc_now(),
                UploadRecord.status.in_(["expired", "aborted", "committed"]),
            ))

    async def migrate_legacy(self) -> int:
        if self.uploads_dir is None or not self.uploads_dir.is_dir():
            return 0
        imported = 0
        for path in self.uploads_dir.glob("*.json"):
            try:
                payload = await asyncio.to_thread(read_json_file, path)
                if not isinstance(payload, dict) or str(payload.get("id")) != path.stem:
                    raise ValueError("Upload filename and identity do not match.")
                async with self.database.unit_of_work() as db:
                    if await db.get(SessionRecord, str(payload.get("sessionId"))) is None:
                        raise ValueError("Upload refers to a missing session.")
                    if await db.get(UploadRecord, str(payload["id"])) is None:
                        await self.write(payload)
                        imported += 1
                await asyncio.to_thread(path.rename, path.with_suffix(".json.migrated"))
            except (ValueError, OSError):
                logger.warning("Legacy upload was not imported: %s", path.name, exc_info=True)
        return imported
