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


async def read_artifact_payload(output: dict | None) -> dict:
    """The artifact's content: the file on disk, or an embedded copy.

    The file wins. Every producer in this codebase writes the artifact to disk
    and records only its metadata on the session, so a dict that also carries an
    inline ``payload`` is either a session written before that was true or a
    fixture — in both cases the file, when there is one, is the current
    document. ``AssessmentService`` used to ask for the opposite ("prefer the
    embedded copy"), which meant the rows persisted for analytics could be built
    from a stale inline snapshot while every other reader saw the file.
    """
    output = output or {}
    legacy_payload = output.get("payload")
    raw_path = output.get("absolutePath")
    if raw_path and Path(raw_path).is_file():
        raw = await asyncio.to_thread(Path(raw_path).read_text, encoding="utf-8")
        return extract_json_object(raw)
    if isinstance(legacy_payload, dict):
        return legacy_payload
    raise FileNotFoundError("Output artifact is not available.")
