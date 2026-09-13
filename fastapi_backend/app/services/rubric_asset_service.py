from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.repositories.rubric_asset_repository import RubricAssetRegistration, RubricAssetRepository


class RubricAssetService:
    def __init__(self, repository: RubricAssetRepository) -> None:
        self.repository = repository

    async def register_case_study_storage_ref(
        self,
        *,
        storage_ref: dict[str, Any],
        original_name: str,
        safe_name: str,
        public_url: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        path = Path(str(storage_ref.get("localPath") or ""))
        registration = await self.repository.register_file(
            path=path,
            rubric_type="case_study",
            original_name=original_name or safe_name or path.name,
            mime_type=storage_ref.get("mimeType"),
            storage_provider=str(storage_ref.get("provider") or "local"),
            storage_key=storage_ref.get("key"),
            public_url=public_url,
            storage_ref=storage_ref,
        )
        await self._remove_duplicate_upload(registration, path)
        asset = registration.asset
        canonical_ref = dict(asset.get("storageRef") or {})
        if not canonical_ref:
            canonical_ref = dict(storage_ref)
            canonical_ref["localPath"] = asset["absolutePath"]
            canonical_ref["sizeBytes"] = asset["sizeBytes"]
            canonical_ref["checksumSha256"] = asset["contentSha256"]
            canonical_ref["etag"] = asset["contentSha256"]
            canonical_ref["key"] = asset.get("storageKey")
        return canonical_ref, asset, registration.is_duplicate

    async def register_communication_rubric(
        self,
        *,
        path: Path,
        original_name: str,
        mime_type: str | None,
    ) -> RubricAssetRegistration:
        return await self.repository.register_file(
            path=path,
            rubric_type="communication",
            original_name=original_name or path.name,
            mime_type=mime_type,
        )

    @staticmethod
    async def _remove_duplicate_upload(registration: RubricAssetRegistration, uploaded_path: Path) -> None:
        if not registration.is_duplicate:
            return
        canonical_path = Path(str(registration.asset.get("absolutePath") or ""))
        if canonical_path == uploaded_path:
            return
        await asyncio.to_thread(lambda: uploaded_path.unlink(missing_ok=True))
