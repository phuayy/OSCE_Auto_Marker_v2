"""Google Cloud Storage object storage.

The deployment backend. Two properties make it steadier than relaying bytes
through the API process:

* the browser PUTs straight at a **resumable upload session URI** that this
  server created, so a 2 GB transfer no longer dies when the API restarts, and
  an interrupted transfer resumes at its last committed offset instead of
  starting over;
* the object outlives the process that received it, so a job retried on another
  worker re-reads the same source rather than finding a half-written temp file.

Nothing here ever fetches a location supplied by a caller. The client is handed
a session URI minted from a key *this server* derived from the session id, and
at completion the server re-reads the object by that same key. A request body
carrying its own ``source_url`` would be a server-side request forgery
primitive; the key round-trip gives the same workflow without one.

Because ffmpeg, WhisperX and the scoring subprocesses are path-based,
``materialize`` streams a committed object into a local cache and verifies its
SHA-256 while doing so. The cache is keyed by object key, so a retried job on a
worker that already holds the file skips the download entirely.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4


from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.utils import atomic_replace, sanitize_file_name
from app.domain.enums import UploadStatus
from app.storage.base import (
    PreparedUploadFile,
    build_object_key,
    build_storage_ref,
    find_upload_file,
    mark_file_committed,
)


logger = logging.getLogger(__name__)

_HASH_CHUNK_BYTES = 8 * 1024 * 1024


class GcsObjectStorageService:
    provider = "gcs"
    # Tells the browser to PUT the bytes at `uploadUrl` with Content-Range
    # headers rather than relaying parts through /api/uploads/{id}/parts/{n}.
    strategy = "gcs_resumable"

    def __init__(self, settings: Settings) -> None:
        if not settings.gcs_bucket:
            raise AppError("STORAGE_BACKEND=gcs requires GCS_BUCKET to be set.", status_code=500)
        self.settings = settings
        self._bucket: Any | None = None
        self._bucket_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Client plumbing
    # ------------------------------------------------------------------

    async def _get_bucket(self) -> Any:
        """The bucket handle, built once per process.

        The SDK is imported lazily so a local-backend deployment never needs
        google-cloud-storage installed, and constructing the client (which reads
        credentials from disk or the metadata server) happens off the event loop.
        """
        if self._bucket is not None:
            return self._bucket
        async with self._bucket_lock:
            if self._bucket is not None:
                return self._bucket
            self._bucket = await asyncio.to_thread(self._build_bucket)
            return self._bucket

    def _client(self) -> Any:
        try:
            from google.cloud import storage  # type: ignore[import-not-found]
        except Exception as error:  # pragma: no cover - depends on deployment extras
            raise AppError(
                "STORAGE_BACKEND=gcs requires the google-cloud-storage package. "
                "Install it, or set STORAGE_BACKEND=local.",
                status_code=500,
            ) from error
        if self.settings.gcs_project:
            return storage.Client(project=self.settings.gcs_project)
        return storage.Client()

    def _build_bucket(self) -> Any:
        return self._client().bucket(self.settings.gcs_bucket)

    async def _blob(self, key: str) -> Any:
        bucket = await self._get_bucket()
        return bucket.blob(self._safe_key(key))

    @staticmethod
    def _safe_key(key: str) -> str:
        """Reject anything that is not a plain forward-slash object key.

        Every key this backend uses is server-minted, so a violation means a
        stored record was tampered with or a caller reached a method it should
        not have. Failing here also stops a doctored key from escaping the cache
        directory when the object is materialised.
        """
        normalized = str(key or "").replace("\\", "/").strip("/")
        if not normalized or ".." in normalized.split("/"):
            raise AppError("Invalid object key.", status_code=400)
        return normalized

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def ensure_layout(self) -> None:
        """Confirm the bucket is reachable and create the local object cache.

        Done at startup so a missing bucket or a bad credential surfaces as a
        boot failure rather than as a failed upload an hour later.
        """
        await asyncio.to_thread(self.settings.gcs_cache_root.mkdir, parents=True, exist_ok=True)
        bucket = await self._get_bucket()
        exists = await asyncio.to_thread(bucket.exists)
        if not exists:
            raise AppError(
                f"GCS bucket '{self.settings.gcs_bucket}' is not reachable with the configured credentials.",
                status_code=500,
            )

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

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
        safe_name = sanitize_file_name(original_name)
        key = build_object_key(
            object_prefix=self.settings.object_prefix,
            session_id=session_id,
            kind=kind,
            safe_name=safe_name,
        )
        blob = await self._blob(key)
        content_type = mime_type or "application/octet-stream"
        # `origin` is what makes the browser's cross-origin PUT survive CORS.
        # Without GCS_UPLOAD_ORIGIN the session is still created, but only a
        # same-origin or non-browser client can use it.
        session_uri = await asyncio.to_thread(
            blob.create_resumable_upload_session,
            content_type=content_type,
            size=size_bytes or None,
            origin=self.settings.gcs_upload_origin or None,
        )
        return PreparedUploadFile(
            file_id=file_id,
            kind=kind,
            key=key,
            safe_name=safe_name,
            original_name=original_name,
            mime_type=content_type,
            size_bytes=size_bytes,
            checksum_sha256=checksum_sha256,
            strategy=self.strategy,
            part_size_bytes=self.settings.upload_part_size_bytes,
            upload_url=str(session_uri),
        )

    async def put_part(
        self, upload: dict[str, Any], file_id: str, part_number: int, body: bytes
    ) -> dict[str, Any]:
        """Not used by this backend — parts go straight to the bucket.

        Kept on the interface, and answered with a precise error, so a client
        built against the local backend fails loudly instead of appearing to
        upload into a void.
        """
        find_upload_file(upload, file_id)
        raise AppError(
            "This deployment uploads directly to Google Cloud Storage. "
            "PUT the file at the `uploadUrl` returned by /api/uploads/initiate.",
            status_code=409,
        )

    async def complete_file(self, upload: dict[str, Any], file_record: dict[str, Any]) -> dict[str, Any]:
        """Verify the client's direct upload and commit it.

        The only thing trusted from the client is "I finished". The object is
        re-read by the server-minted key, its length checked against the
        declaration made at initiate, and its SHA-256 computed from the bytes
        actually stored. A declared checksum that does not match is a hard
        failure — the same guarantee the local backend gives at assembly time.
        """
        if file_record.get("status") == UploadStatus.COMMITTED and file_record.get("storageRef"):
            return dict(file_record["storageRef"])

        key = self._safe_key(str(file_record["key"]))
        expected_size = int(file_record.get("sizeBytes") or 0)
        bucket = await self._get_bucket()
        blob = await asyncio.to_thread(bucket.get_blob, key)
        if blob is None:
            raise AppError(
                f"{file_record.get('kind')} upload was never received by object storage.",
                status_code=400,
            )
        actual_size = int(blob.size or 0)
        if expected_size and actual_size != expected_size:
            raise AppError(
                f"{file_record.get('kind')} upload is incomplete ({actual_size}/{expected_size} bytes).",
                status_code=400,
            )

        cached_path, digest = await self._download_to_cache(blob, key)
        expected_sha = file_record.get("checksumSha256")
        if expected_sha and str(expected_sha).lower() != digest:
            raise AppError(f"{file_record.get('kind')} checksum mismatch.", status_code=400)

        storage_ref = self._storage_ref(
            key=key,
            size_bytes=actual_size,
            mime_type=blob.content_type or file_record.get("mimeType") or "",
            digest=digest,
            local_path=str(cached_path),
            generation=self._blob_generation(blob),
        )
        mark_file_committed(file_record, storage_ref, actual_size)
        return storage_ref

    async def abort_upload(self, upload: dict[str, Any]) -> None:
        """Delete every object this upload may have created.

        A resumable session that was never finalized leaves nothing behind, so
        missing blobs are expected and ignored; only committed-then-abandoned
        files actually need removing.
        """
        bucket = await self._get_bucket()
        for file_record in upload.get("files") or []:
            raw_key = str(file_record.get("key") or "")
            if not raw_key:
                continue
            try:
                key = self._safe_key(raw_key)
            except AppError:
                continue
            try:
                await asyncio.to_thread(bucket.delete_blob, key)
            except Exception:
                # Includes NotFound for a session that never completed.
                logger.debug("Nothing to delete for aborted upload object %s.", key)
            await asyncio.to_thread(self._cache_path(key).unlink, True)

    # ------------------------------------------------------------------
    # Read side
    # ------------------------------------------------------------------

    def public_url_for_key(self, key: str) -> str:
        """A time-limited V4 signed URL the browser can read the object from.

        Signing needs a credential that can sign: a service-account key file, or
        IAM ``signBlob`` permission on the runtime service account.
        """
        normalized = self._safe_key(key)
        try:
            blob = self._client().bucket(self.settings.gcs_bucket).blob(normalized)
            return str(
                blob.generate_signed_url(
                    version="v4",
                    expiration=timedelta(seconds=max(60, self.settings.gcs_signed_url_ttl_seconds)),
                    method="GET",
                )
            )
        except AppError:
            raise
        except Exception as error:
            raise AppError(
                "Could not sign a Google Cloud Storage URL. The runtime credential must be able to "
                "sign (a service-account key, or iam.serviceAccounts.signBlob on the attached account).",
                status_code=500,
            ) from error

    async def materialize(self, storage_ref: dict[str, Any]) -> Path:
        """Local path holding the object's bytes, downloading only if needed.

        A cached copy is accepted when its length matches the committed ref. The
        ref's SHA-256 was verified against the stored bytes at commit time, and
        re-hashing gigabytes at every job start would cost more than the download
        it is meant to avoid. Anything shorter or longer than the recorded length
        — a download killed mid-flight, say — is discarded and fetched again.
        """
        key = self._safe_key(str(storage_ref.get("key") or ""))
        expected_size = int(storage_ref.get("sizeBytes") or 0)
        cached_path = self._cache_path(key)

        def _cache_is_usable() -> bool:
            return cached_path.is_file() and (
                expected_size <= 0 or cached_path.stat().st_size == expected_size
            )

        if await asyncio.to_thread(_cache_is_usable):
            return cached_path

        bucket = await self._get_bucket()
        blob = await asyncio.to_thread(bucket.get_blob, key)
        if blob is None:
            raise AppError(f"Stored object is missing from the bucket: {key}", status_code=500)
        cached_path, digest = await self._download_to_cache(blob, key)
        expected_sha = str(storage_ref.get("checksumSha256") or "").lower()
        if expected_sha and expected_sha != digest:
            await asyncio.to_thread(cached_path.unlink, True)
            raise AppError(f"Stored object failed checksum verification: {key}", status_code=500)
        return cached_path

    async def prepare_session_sources(self, session: dict[str, Any]) -> dict[str, Any]:
        """Pull this session's sources onto local disk before a job runs them.

        Called at the head of every job execution, so a job retried on a worker
        that has never seen the session fetches what it needs, while a retry on
        the same worker reuses the cache.
        """
        files = session.get("files") or {}
        for kind in ("video", "caseStudy"):
            file_meta = files.get(kind) or {}
            storage_ref = file_meta.get("storageRef")
            if not isinstance(storage_ref, dict) or not storage_ref.get("key"):
                continue
            local_path = await self.materialize(storage_ref)
            file_meta["absolutePath"] = str(local_path)
            storage_ref["localPath"] = str(local_path)
        return session

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _cache_path(self, key: str) -> Path:
        return self.settings.gcs_cache_root / self._safe_key(key)

    async def _download_to_cache(self, blob: Any, key: str) -> tuple[Path, str]:
        """Stream a blob into the object cache, returning its path and SHA-256.

        Written under a temp name and moved into place, so a killed download can
        never leave a truncated file that a later run would mistake for a
        complete one.
        """
        cached_path = self._cache_path(key)
        tmp_path = cached_path.with_name(f".{cached_path.name}.{uuid4().hex}.partial")

        def _download() -> str:
            cached_path.parent.mkdir(parents=True, exist_ok=True)
            hasher = hashlib.sha256()
            try:
                with tmp_path.open("wb") as target:
                    blob.download_to_file(target)
                with tmp_path.open("rb") as source:
                    for chunk in iter(lambda: source.read(_HASH_CHUNK_BYTES), b""):
                        hasher.update(chunk)
                atomic_replace(tmp_path, cached_path)
                return hasher.hexdigest()
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise

        digest = await asyncio.to_thread(_download)
        return cached_path, digest

    def _storage_ref(
        self,
        *,
        key: str,
        size_bytes: int,
        mime_type: str,
        digest: str,
        local_path: str | None,
        generation: str | None,
    ) -> dict[str, Any]:
        return build_storage_ref(
            provider=self.provider,
            bucket=self.settings.gcs_bucket,
            key=key,
            uri=f"gs://{self.settings.gcs_bucket}/{key}",
            local_path=local_path,
            size_bytes=size_bytes,
            mime_type=mime_type,
            digest=digest,
            generation=generation,
        )

    @staticmethod
    def _blob_generation(blob: Any) -> str | None:
        generation = getattr(blob, "generation", None)
        return str(generation) if generation is not None else None


__all__ = ["GcsObjectStorageService"]
