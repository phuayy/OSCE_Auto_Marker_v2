from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.core.json_utils import read_json_file, write_json_file


class UploadRepository:
    def __init__(self, uploads_dir: Path) -> None:
        self.uploads_dir = uploads_dir

    def get_path(self, upload_id: str) -> Path:
        return self.uploads_dir / f"{upload_id}.json"

    async def read(self, upload_id: str) -> dict[str, Any]:
        def _read() -> dict[str, Any]:
            payload = read_json_file(self.get_path(upload_id))
            if payload is None:
                raise FileNotFoundError(f"Upload not found: {upload_id}")
            return payload

        return await asyncio.to_thread(_read)

    async def write(self, upload: dict[str, Any]) -> None:
        await asyncio.to_thread(write_json_file, self.get_path(str(upload["id"])), upload)

    async def read_all(self) -> list[dict[str, Any]]:
        def _read_all() -> list[dict[str, Any]]:
            if not self.uploads_dir.exists():
                return []
            uploads: list[dict[str, Any]] = []
            for path in self.uploads_dir.glob("*.json"):
                payload = read_json_file(path)
                if isinstance(payload, dict):
                    uploads.append(payload)
            return uploads

        return await asyncio.to_thread(_read_all)
