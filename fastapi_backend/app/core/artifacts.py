from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TypedDict

from app.core.json_utils import extract_json_object


class ArtifactMetadata(TypedDict):
    fileName: str
    absolutePath: str
    url: str
    sizeBytes: int


def artifact_metadata(path: Path, media_directory: str) -> ArtifactMetadata:
    return {
        "fileName": path.name,
        "absolutePath": str(path),
        "url": f"{media_directory.rstrip('/')}/{path.name}",
        "sizeBytes": path.stat().st_size,
    }


async def read_artifact_payload(output: dict | None, *, prefer_legacy: bool = False) -> dict:
    output = output or {}
    legacy_payload = output.get("payload")
    if prefer_legacy and isinstance(legacy_payload, dict):
        return legacy_payload
    raw_path = output.get("absolutePath")
    if raw_path and Path(raw_path).is_file():
        raw = await asyncio.to_thread(Path(raw_path).read_text, encoding="utf-8")
        return extract_json_object(raw)
    if isinstance(legacy_payload, dict):
        return legacy_payload
    raise FileNotFoundError("Output artifact is not available.")
