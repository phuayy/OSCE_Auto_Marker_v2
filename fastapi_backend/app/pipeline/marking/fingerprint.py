"""What a content sheet was marked *from*.

A sheet records which model produced it and which files it names
(``transcript_file``, ``case_study_file``), but a path is not content:
re-transcribing a session writes a new transcript to the *same* path. This
module answers the other question — are the bytes behind those paths the bytes
this run is about to hand the marker?

The signature is the file's content, not its location: size plus a SHA-256 of
the bytes, with no path and no mtime. A transcript restored from a backup or
materialised into the GCS object cache is the same input at a different path
with a different ``mtime_ns``; hashing it costs milliseconds against a marker
call that costs minutes, and it never claims two different transcripts are one.
Where it cannot tell, it says "different": a false refresh costs one re-mark, a
false reuse marks a different recording of the conversation.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Where the signature lives on a sheet, and the shape it is in. A sheet from a
# build before this key existed carries none and is never reused.
SHEET_INPUTS_KEY = "input_signature"
SHEET_INPUTS_SCHEMA = "content-inputs-v1"

_CHUNK_BYTES = 1024 * 1024


def file_signature(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(_CHUNK_BYTES), b""):
            size += len(chunk)
            digest.update(chunk)
    return {"size_bytes": size, "sha256": digest.hexdigest()}


def input_signature(*, transcript_path: Path, case_study_path: Path) -> dict[str, Any]:
    """Blocking (it reads both files); call it through ``asyncio.to_thread``."""
    return {
        "schema": SHEET_INPUTS_SCHEMA,
        "transcript": file_signature(transcript_path),
        "case_study": file_signature(case_study_path),
    }


def sheet_marked_from(payload: Any, expected: Mapping[str, Any] | None) -> bool:
    """Was this sheet marked from exactly these inputs?

    ``False`` when the sheet carries no signature — sheets from a build before
    this check existed are refreshed, not trusted — and ``False`` when the
    caller could not compute one. Both directions fail toward re-marking.
    """
    if expected is None or not isinstance(payload, Mapping):
        return False
    recorded = payload.get(SHEET_INPUTS_KEY)
    if not isinstance(recorded, Mapping):
        return False
    return dict(recorded) == dict(expected)


__all__ = [
    "SHEET_INPUTS_KEY",
    "SHEET_INPUTS_SCHEMA",
    "file_signature",
    "input_signature",
    "sheet_marked_from",
]
