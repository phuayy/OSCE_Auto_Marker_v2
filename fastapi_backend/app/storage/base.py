"""Object-storage contract shared by every backend.

The application never touches a provider SDK directly: it holds an
``ObjectStorage`` and works in terms of *storage refs* — small dictionaries that
name an object by ``provider``/``bucket``/``key`` plus its verified size and
SHA-256. A ref is always minted by the server. Nothing downstream ever accepts a
caller-supplied URL or path and fetches it, which is what keeps the "hand the
backend a source location" flow free of server-side request forgery.

Two methods carry the local/remote split:

``prepare_upload_file`` describes how the browser should send bytes — through
this API for the local backend, straight at the bucket for a cloud one — and
``materialize`` guarantees a local path for a committed object, because ffmpeg,
WhisperX and the scoring subprocesses are all path-based and cannot read a
bucket.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from fastapi import UploadFile

from app.core.exceptions import AppError
from app.core.utils import utc_now_iso
from app.domain.enums import UploadStatus


SourceFileKind = Literal["video", "caseStudy"]


@dataclass(frozen=True)
class PreparedUploadFile:
    """The upload plan for one file, returned to the browser at initiate time.

    ``strategy`` tells the client which transport to use. ``part_url_template``
    is set when parts flow through this API; ``upload_url`` is set when the
    client uploads straight to the provider. Exactly one of them is populated.
    """

    file_id: str
    kind: str
    key: str
    safe_name: str
    original_name: str
    mime_type: str
    size_bytes: int
    checksum_sha256: str | None
    strategy: str
    part_size_bytes: int
    part_url_template: str | None = None
    upload_url: str | None = None

    def to_response(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "fileId": self.file_id,
            "kind": self.kind,
            "key": self.key,
            "strategy": self.strategy,
            "partSizeBytes": self.part_size_bytes,
            "sizeBytes": self.size_bytes,
            "mimeType": self.mime_type,
            "originalName": self.original_name,
        }
        if self.part_url_template:
            payload["partUrlTemplate"] = self.part_url_template
        if self.upload_url:
            payload["uploadUrl"] = self.upload_url
        return payload


@runtime_checkable
class ObjectStorage(Protocol):
    """What the upload, job and pipeline layers require of a storage backend."""

    provider: str
    strategy: str

    async def ensure_layout(self) -> None:
        """Create whatever the backend needs before the first upload."""

    async def prepare_upload_file(
        self,
        *,
        upload_id: str,
        session_id: str,
        file_id: str,
        kind: str,
        original_name: str,
        mime_type: str,
        size_bytes: int,
        checksum_sha256: str | None,
    ) -> PreparedUploadFile:
        """Mint the server-side key and the transport plan for one file."""

    async def save_uploaded_source(
        self,
        upload: UploadFile,
        *,
        session_id: str,
        kind: SourceFileKind,
        max_bytes: int,
    ) -> dict[str, Any]:
        """Store a single-shot multipart upload (the legacy /api/upload route)."""

    async def put_part(
        self, upload: dict[str, Any], file_id: str, part_number: int, body: bytes
    ) -> dict[str, Any]:
        """Accept one chunk relayed through this API."""

    async def complete_file(self, upload: dict[str, Any], file_record: dict[str, Any]) -> dict[str, Any]:
        """Finalize one file and return its committed storage ref.

        Must verify the object actually holds the declared byte count and, when
        the client declared one, the declared SHA-256.
        """

    async def abort_upload(self, upload: dict[str, Any]) -> None:
        """Discard partial data for an abandoned upload."""

    def public_url_for_key(self, key: str) -> str:
        """A URL the browser can read the object from."""

    async def materialize(self, storage_ref: dict[str, Any]) -> Path:
        """Return a local path holding the object's bytes, fetching if needed."""

    async def prepare_session_sources(self, session: dict[str, Any]) -> dict[str, Any]:
        """Point the session's source files at local paths a worker can open."""


def build_object_key(*, object_prefix: str, session_id: str, kind: str, safe_name: str) -> str:
    prefix = f"{object_prefix}/" if object_prefix else ""
    return f"{prefix}sessions/{session_id}/source/{kind}/{safe_name}"


def build_storage_ref(
    *,
    provider: str,
    bucket: str | None,
    key: str,
    uri: str,
    size_bytes: int,
    mime_type: str,
    digest: str,
    local_path: str | None = None,
    etag: str | None = None,
    generation: str | None = None,
) -> dict[str, Any]:
    """The canonical committed-object record persisted on a session."""
    return {
        "provider": provider,
        "bucket": bucket,
        "key": key,
        "uri": uri,
        "localPath": local_path,
        "sizeBytes": size_bytes,
        "mimeType": mime_type,
        "checksumSha256": digest,
        "etag": etag or digest,
        "generation": generation,
        "status": UploadStatus.COMMITTED,
        "committedAt": utc_now_iso(),
    }


def hash_path(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def find_upload_file(upload: dict[str, Any], file_id: str) -> dict[str, Any]:
    for file_record in upload.get("files") or []:
        if str(file_record.get("fileId")) == str(file_id):
            return file_record
    raise AppError("Upload file not found.", status_code=404)


def mark_file_committed(
    file_record: dict[str, Any],
    storage_ref: dict[str, Any],
    expected_size: int,
) -> None:
    file_record["status"] = UploadStatus.COMMITTED
    file_record["uploadedBytes"] = expected_size
    file_record["storageRef"] = storage_ref


__all__ = [
    "ObjectStorage",
    "PreparedUploadFile",
    "SourceFileKind",
    "build_object_key",
    "build_storage_ref",
    "find_upload_file",
    "hash_path",
    "mark_file_committed",
]
