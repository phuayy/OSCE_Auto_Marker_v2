from __future__ import annotations

import asyncio
import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import UploadFile

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.utils import atomic_replace, sanitize_file_name, utc_now_iso


SourceFileKind = Literal["video", "caseStudy"]


@dataclass(frozen=True)
class PreparedUploadFile:
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
    part_url_template: str | None
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


class LocalObjectStorageService:
    provider = "local"
    strategy = "local_multipart"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def prepare_session_sources(self, session: dict[str, Any]) -> dict[str, Any]:
        video = (session.get("files") or {}).get("video") or {}
        storage_ref = video.get("storageRef") or {}
        local_path = storage_ref.get("localPath")
        if local_path:
            video["absolutePath"] = str(local_path)
        return session

    async def ensure_layout(self) -> None:
        await asyncio.to_thread(self.settings.object_storage_root.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread((self.settings.object_storage_root / ".uploads").mkdir, parents=True, exist_ok=True)

    def prepare_upload_file(
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
        safe_name = sanitize_file_name(original_name)
        key = self._object_key(session_id=session_id, kind=kind, safe_name=safe_name)
        return PreparedUploadFile(
            file_id=file_id,
            kind=kind,
            key=key,
            safe_name=safe_name,
            original_name=original_name,
            mime_type=mime_type,
            size_bytes=size_bytes,
            checksum_sha256=checksum_sha256,
            strategy=self.strategy,
            part_size_bytes=self.settings.upload_part_size_bytes,
            part_url_template=f"/api/uploads/{upload_id}/parts/{{partNumber}}?fileId={file_id}",
        )

    async def save_uploaded_source(
        self,
        upload: UploadFile,
        *,
        session_id: str,
        kind: SourceFileKind,
        max_bytes: int,
    ) -> dict[str, Any]:
        safe_name = sanitize_file_name(upload.filename or "file")
        key = self._object_key(session_id=session_id, kind=kind, safe_name=safe_name)
        final_path = self.settings.object_storage_root / key
        final_path.parent.mkdir(parents=True, exist_ok=True)

        def _copy() -> tuple[str, int]:
            upload.file.seek(0)
            hasher = hashlib.sha256()
            copied = 0
            tmp_path = final_path.with_name(f".tmp-{uuid4().hex[:8]}")
            try:
                with tmp_path.open("wb") as buffer:
                    while True:
                        chunk = upload.file.read(1024 * 1024)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > max_bytes:
                            raise AppError(
                                f"Uploaded file exceeds the {max_bytes // (1024 * 1024)} MB limit.",
                                status_code=413,
                            )
                        hasher.update(chunk)
                        buffer.write(chunk)
                atomic_replace(tmp_path, final_path)
                return hasher.hexdigest(), copied
            except Exception:
                tmp_path.unlink(missing_ok=True)
                final_path.unlink(missing_ok=True)
                raise

        digest, size_bytes = await asyncio.to_thread(_copy)
        storage_ref = self._storage_ref(
            key=key,
            path=final_path,
            size_bytes=size_bytes,
            mime_type=upload.content_type or "",
            digest=digest,
        )
        return {
            "originalName": upload.filename or safe_name,
            "fileName": safe_name,
            "absolutePath": str(final_path),
            "url": self.public_url_for_key(key),
            "sizeBytes": size_bytes,
            "mimeType": upload.content_type or "",
            "storageRef": storage_ref,
        }

    async def put_part(self, upload: dict[str, Any], file_id: str, part_number: int, body: bytes) -> dict[str, Any]:
        if part_number < 1:
            raise AppError("partNumber must be greater than zero.", status_code=400)
        if upload.get("status") not in {"initiated", "uploading"}:
            raise AppError("Upload is not accepting parts.", status_code=409)
        file_record = self._find_file(upload, file_id)
        if file_record.get("status") in {"committed", "aborted"}:
            raise AppError("File upload is already finalized.", status_code=409)
        if len(body) > self.settings.upload_part_size_bytes:
            raise AppError("Uploaded part exceeds configured part size.", status_code=413)

        part_path = self._part_path(str(upload["id"]), file_id, part_number)
        part_path.parent.mkdir(parents=True, exist_ok=True)

        def _write() -> str:
            digest = hashlib.sha256(body).hexdigest()
            tmp_path = part_path.with_name(f".tmp-{uuid4().hex[:8]}")
            try:
                tmp_path.write_bytes(body)
                atomic_replace(tmp_path, part_path)
            finally:
                tmp_path.unlink(missing_ok=True)
            return digest

        digest = await asyncio.to_thread(_write)
        parts = [part for part in file_record.get("parts", []) if int(part.get("partNumber") or 0) != part_number]
        parts.append(
            {
                "partNumber": part_number,
                "sizeBytes": len(body),
                "sha256": digest,
                "createdAt": utc_now_iso(),
            }
        )
        parts.sort(key=lambda item: int(item.get("partNumber") or 0))
        file_record["parts"] = parts
        file_record["uploadedBytes"] = sum(int(part.get("sizeBytes") or 0) for part in parts)
        file_record["status"] = "uploading"
        upload["status"] = "uploading"
        return {
            "fileId": file_id,
            "partNumber": part_number,
            "sizeBytes": len(body),
            "sha256": digest,
            "uploadedBytes": file_record["uploadedBytes"],
        }

    async def complete_file(self, upload: dict[str, Any], file_record: dict[str, Any]) -> dict[str, Any]:
        if file_record.get("status") == "committed" and file_record.get("storageRef"):
            return dict(file_record["storageRef"])

        expected_size = int(file_record.get("sizeBytes") or 0)
        final_path = self.settings.object_storage_root / str(file_record["key"])
        existing_ref = await asyncio.to_thread(
            self._existing_final_storage_ref,
            file_record=file_record,
            final_path=final_path,
            expected_size=expected_size,
        )
        if existing_ref is not None:
            self._mark_file_committed(file_record, existing_ref, expected_size)
            return existing_ref

        parts = sorted(file_record.get("parts") or [], key=lambda item: int(item.get("partNumber") or 0))
        uploaded_size = sum(int(part.get("sizeBytes") or 0) for part in parts)
        if uploaded_size != expected_size:
            raise AppError(
                f"{file_record.get('kind')} upload is incomplete ({uploaded_size}/{expected_size} bytes).",
                status_code=400,
            )
        if not parts:
            raise AppError(f"{file_record.get('kind')} upload has no parts.", status_code=400)

        final_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = final_path.with_name(f".{final_path.name}.{uuid4().hex}.assembling")

        def _assemble() -> str:
            hasher = hashlib.sha256()
            try:
                with tmp_path.open("wb") as target:
                    for part in parts:
                        part_path = self._part_path(
                            str(upload["id"]),
                            str(file_record["fileId"]),
                            int(part["partNumber"]),
                        )
                        if not part_path.exists():
                            raise FileNotFoundError(f"Missing upload part {part['partNumber']}.")
                        with part_path.open("rb") as source:
                            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                                hasher.update(chunk)
                                target.write(chunk)
                digest = hasher.hexdigest()
                expected_sha = file_record.get("checksumSha256")
                if expected_sha and str(expected_sha).lower() != digest:
                    raise AppError(f"{file_record.get('kind')} checksum mismatch.", status_code=400)
                atomic_replace(tmp_path, final_path)
                return digest
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise

        try:
            digest = await asyncio.to_thread(_assemble)
        except FileNotFoundError:
            existing_ref = await asyncio.to_thread(
                self._existing_final_storage_ref,
                file_record=file_record,
                final_path=final_path,
                expected_size=expected_size,
            )
            if existing_ref is None:
                raise
            self._mark_file_committed(file_record, existing_ref, expected_size)
            return existing_ref

        storage_ref = self._storage_ref(
            key=str(file_record["key"]),
            path=final_path,
            size_bytes=expected_size,
            mime_type=file_record.get("mimeType") or "",
            digest=digest,
        )
        self._mark_file_committed(file_record, storage_ref, expected_size)
        return storage_ref

    async def abort_upload(self, upload: dict[str, Any]) -> None:
        upload_dir = self.settings.object_storage_root / ".uploads" / str(upload["id"])
        await asyncio.to_thread(lambda: shutil.rmtree(upload_dir, ignore_errors=True))

    def public_url_for_key(self, key: str) -> str:
        normalized_key = key.replace("\\", "/")
        return f"/media/source/{normalized_key}"

    def _object_key(self, *, session_id: str, kind: str, safe_name: str) -> str:
        prefix = f"{self.settings.object_prefix}/" if self.settings.object_prefix else ""
        return f"{prefix}sessions/{session_id}/source/{kind}/{safe_name}"

    def _storage_ref(
        self,
        *,
        key: str,
        path: Path,
        size_bytes: int,
        mime_type: str,
        digest: str,
    ) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "bucket": None,
            "key": key,
            "uri": f"file://{path}",
            "localPath": str(path),
            "sizeBytes": size_bytes,
            "mimeType": mime_type,
            "checksumSha256": digest,
            "etag": digest,
            "generation": None,
            "status": "committed",
            "committedAt": utc_now_iso(),
        }

    def _existing_final_storage_ref(
        self,
        *,
        file_record: dict[str, Any],
        final_path: Path,
        expected_size: int,
    ) -> dict[str, Any] | None:
        if not final_path.exists() or not final_path.is_file():
            return None
        actual_size = final_path.stat().st_size
        if actual_size != expected_size:
            return None

        digest = self._hash_path(final_path)
        expected_sha = file_record.get("checksumSha256")
        if expected_sha and str(expected_sha).lower() != digest:
            return None
        return self._storage_ref(
            key=str(file_record["key"]),
            path=final_path,
            size_bytes=expected_size,
            mime_type=file_record.get("mimeType") or "",
            digest=digest,
        )

    @staticmethod
    def _hash_path(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    @staticmethod
    def _mark_file_committed(
        file_record: dict[str, Any],
        storage_ref: dict[str, Any],
        expected_size: int,
    ) -> None:
        file_record["status"] = "committed"
        file_record["uploadedBytes"] = expected_size
        file_record["storageRef"] = storage_ref

    def _part_path(self, upload_id: str, file_id: str, part_number: int) -> Path:
        return self.settings.object_storage_root / ".uploads" / upload_id / file_id / f"part-{part_number:06d}"

    @staticmethod
    def _find_file(upload: dict[str, Any], file_id: str) -> dict[str, Any]:
        for file_record in upload.get("files") or []:
            if str(file_record.get("fileId")) == str(file_id):
                return file_record
        raise AppError("Upload file not found.", status_code=404)


def create_storage_service(settings: Settings) -> LocalObjectStorageService:
    if settings.storage_backend != "local":
        raise AppError(
            f"Unsupported STORAGE_BACKEND: {settings.storage_backend}. Only 'local' is implemented.",
            status_code=500,
        )
    return LocalObjectStorageService(settings)
