from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.utils import sanitize_file_name
from app.database.models import RubricAsset, utc_now
from app.database.orm import OrmDatabase


@dataclass(frozen=True)
class RubricAssetRegistration:
    asset: dict[str, Any]
    is_duplicate: bool


class RubricAssetRepository:
    def __init__(self, database: OrmDatabase) -> None:
        self.database = database

    async def register_file(
        self,
        *,
        path: Path,
        rubric_type: str,
        original_name: str,
        mime_type: str | None = None,
        storage_provider: str = "local",
        storage_key: str | None = None,
        public_url: str | None = None,
        storage_ref: dict[str, Any] | None = None,
    ) -> RubricAssetRegistration:
        digest, size_bytes = await self._hash_file(path)
        normalized_name = sanitize_file_name(original_name or path.name).lower()
        now = utc_now()

        try:
            async with self.database.transaction() as session:
                existing = await session.scalar(
                    self._identity_query(
                        rubric_type=rubric_type,
                        normalized_name=normalized_name,
                        size_bytes=size_bytes,
                        digest=digest,
                    ).with_for_update()
                )
                if existing is not None:
                    existing.times_used += 1
                    existing.last_used_at = now
                    await session.flush()
                    return RubricAssetRegistration(self._to_dict(existing), True)

                asset = RubricAsset(
                    id=str(uuid4()),
                    rubric_type=rubric_type,
                    original_name=original_name or path.name,
                    normalized_name=normalized_name,
                    file_name=path.name,
                    content_sha256=digest,
                    size_bytes=size_bytes,
                    mime_type=mime_type,
                    storage_provider=storage_provider,
                    storage_key=storage_key,
                    absolute_path=str(path),
                    public_url=public_url,
                    storage_ref_json=storage_ref or {},
                    times_used=1,
                    created_at=now,
                    last_used_at=now,
                )
                session.add(asset)
                await session.flush()
                return RubricAssetRegistration(self._to_dict(asset), False)
        except IntegrityError:
            return await self._mark_existing_used(
                rubric_type=rubric_type,
                normalized_name=normalized_name,
                size_bytes=size_bytes,
                digest=digest,
            )

    async def read(self, asset_id: str) -> dict[str, Any] | None:
        async with self.database.session() as session:
            asset = await session.get(RubricAsset, asset_id)
            return self._to_dict(asset) if asset is not None else None

    async def _mark_existing_used(
        self,
        *,
        rubric_type: str,
        normalized_name: str,
        size_bytes: int,
        digest: str,
    ) -> RubricAssetRegistration:
        now = utc_now()
        async with self.database.transaction() as session:
            existing = await session.scalar(
                self._identity_query(
                    rubric_type=rubric_type,
                    normalized_name=normalized_name,
                    size_bytes=size_bytes,
                    digest=digest,
                ).with_for_update()
            )
            if existing is None:
                raise RuntimeError("Rubric asset uniqueness conflict could not be resolved.")
            existing.times_used += 1
            existing.last_used_at = now
            await session.flush()
            return RubricAssetRegistration(self._to_dict(existing), True)

    @staticmethod
    async def _hash_file(path: Path) -> tuple[str, int]:
        def _hash() -> tuple[str, int]:
            hasher = hashlib.sha256()
            size = 0
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    size += len(chunk)
                    hasher.update(chunk)
            return hasher.hexdigest(), size

        return await asyncio.to_thread(_hash)

    @staticmethod
    def _identity_query(*, rubric_type: str, normalized_name: str, size_bytes: int, digest: str) -> Any:
        return select(RubricAsset).where(
            RubricAsset.rubric_type == rubric_type,
            RubricAsset.normalized_name == normalized_name,
            RubricAsset.size_bytes == size_bytes,
            RubricAsset.content_sha256 == digest,
        )

    @staticmethod
    def _to_dict(asset: RubricAsset) -> dict[str, Any]:
        return {
            "id": asset.id,
            "rubricType": asset.rubric_type,
            "originalName": asset.original_name,
            "normalizedName": asset.normalized_name,
            "fileName": asset.file_name,
            "contentSha256": asset.content_sha256,
            "sizeBytes": asset.size_bytes,
            "mimeType": asset.mime_type,
            "storageProvider": asset.storage_provider,
            "storageKey": asset.storage_key,
            "absolutePath": asset.absolute_path,
            "publicUrl": asset.public_url,
            "storageRef": asset.storage_ref_json or {},
            "timesUsed": asset.times_used,
            "createdAt": asset.created_at.isoformat() if asset.created_at else None,
            "lastUsedAt": asset.last_used_at.isoformat() if asset.last_used_at else None,
        }
