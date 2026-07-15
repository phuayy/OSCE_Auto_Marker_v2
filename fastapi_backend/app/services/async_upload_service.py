from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.tasks import BackgroundTaskRegistry
from app.pipeline.media import MediaPipeline
from app.repositories.upload_repository import UploadRepository
from app.schemas.uploads import CompleteUploadRequest, InitiateUploadRequest
from app.services.clip_service import ClipService
from app.services.event_service import EventService
from app.services.job_queue_service import JobQueueService
from app.repositories.video_repository import VideoRepository
from app.services.rubric_asset_service import RubricAssetService
from app.services.session_service import SessionService
from app.core.utils import utc_now_iso
from app.services.storage_service import LocalObjectStorageService


logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}


class AsyncUploadService:
    def __init__(
        self,
        settings: Settings,
        repository: UploadRepository,
        sessions: SessionService,
        storage: LocalObjectStorageService,
        jobs: JobQueueService,
        media: MediaPipeline,
        events: EventService,
        rubric_assets: RubricAssetService,
        videos: VideoRepository,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.sessions = sessions
        self.storage = storage
        self.jobs = jobs
        self.media = media
        self.events = events
        self.rubric_assets = rubric_assets
        self.videos = videos
        self._completion_locks: dict[str, asyncio.Lock] = {}
        self._completion_locks_guard = asyncio.Lock()
        # Holds strong references to background assembly tasks so the event loop
        # cannot garbage-collect them mid-execution (see BackgroundTaskRegistry).
        self._background_tasks = BackgroundTaskRegistry()

    async def initiate(self, payload: InitiateUploadRequest) -> dict[str, Any]:
        self._validate_declared_files(payload)
        session_id = str(uuid4())
        upload_id = str(uuid4())
        expires_at = self._expires_at()
        entries, used_keys = await self.sessions.ensure_names_for_index(await self.sessions.read_all_entries())
        _ = entries

        prepared_files = []
        upload_files = []
        for item in payload.files:
            file_id = str(uuid4())
            prepared = self.storage.prepare_upload_file(
                upload_id=upload_id,
                session_id=session_id,
                file_id=file_id,
                kind=item.kind,
                original_name=item.originalName,
                mime_type=item.mimeType,
                size_bytes=item.sizeBytes,
                checksum_sha256=item.sha256,
            )
            prepared_files.append(prepared)
            upload_files.append(
                {
                    "fileId": prepared.file_id,
                    "kind": prepared.kind,
                    "originalName": prepared.original_name,
                    "safeName": prepared.safe_name,
                    "mimeType": prepared.mime_type,
                    "sizeBytes": prepared.size_bytes,
                    "checksumSha256": prepared.checksum_sha256,
                    "key": prepared.key,
                    "strategy": prepared.strategy,
                    "partSizeBytes": prepared.part_size_bytes,
                    "uploadedBytes": 0,
                    "parts": [],
                    "status": "initiated",
                    "createdAt": utc_now_iso(),
                }
            )

        task_type = "auto_crop" if payload.workflow == "long" else "process_session"
        job = await self.jobs.create_waiting_job(
            session_id,
            task_type,
            payload={"workflow": payload.workflow, "autoProcess": payload.autoProcess, "uploadId": upload_id},
        )
        session = {
            "id": session_id,
            "name": self.sessions.reserve_unique_session_name(used_keys, payload.sessionName or ""),
            "createdAt": utc_now_iso(),
            "status": "waiting_for_upload",
            "workflow": payload.workflow,
            # Long-workflow auto-crop method ("bells" | "person"); None defers
            # to the server default at job execution time.
            "segmentation": payload.segmentation if payload.workflow == "long" else None,
            "upload": {
                "id": upload_id,
                "status": "initiated",
                "strategy": self.storage.strategy,
                "expiresAt": expires_at,
            },
            "job": self.jobs.public_job(job),
            "pipeline": {
                "startedAt": None,
                "endedAt": None,
                "runtimeSeconds": None,
                "mode": self.settings.whisperx_device,
            },
            "files": {
                "video": self._pending_file_meta(upload_files, "video"),
                "caseStudy": self._pending_file_meta(upload_files, "caseStudy"),
            },
            "outputs": ClipService.empty_outputs(),
            "error": None,
        }
        upload = {
            "id": upload_id,
            "sessionId": session_id,
            "workflow": payload.workflow,
            "autoProcess": payload.autoProcess,
            "status": "initiated",
            "strategy": self.storage.strategy,
            "provider": self.storage.provider,
            "createdAt": utc_now_iso(),
            "expiresAt": expires_at,
            "files": upload_files,
            "jobId": job["id"],
        }
        await self.repository.write(upload)
        await self.sessions.write(session)
        await self.events.publish(session_id, "status", {"code": "uploading", "message": "Upload session created."})
        return {
            "session": self.sessions.public_session(session),
            "uploadId": upload_id,
            "strategy": self.storage.strategy,
            "partSizeBytes": self.settings.upload_part_size_bytes,
            "expiresAt": expires_at,
            "fileUploads": [prepared.to_response() for prepared in prepared_files],
            "job": self.jobs.public_job(job),
        }

    async def put_part(self, upload_id: str, file_id: str | None, part_number: int, body: bytes) -> dict[str, Any]:
        upload = await self.repository.read(upload_id)
        self._assert_not_expired(upload)
        resolved_file_id = file_id or self._first_file_id(upload)
        part = await self.storage.put_part(upload, resolved_file_id, part_number, body)
        await self.repository.write(upload)
        await self._sync_pending_session_upload(upload)
        await self.events.publish(
            str(upload["sessionId"]),
            "status",
            {
                "code": "uploading",
                "message": f"Uploaded part {part_number}.",
                "uploadId": upload_id,
                "fileId": resolved_file_id,
            },
        )
        return {"part": part, "upload": self.public_upload(upload)}

    async def status(self, upload_id: str) -> dict[str, Any]:
        upload = await self.repository.read(upload_id)
        if self._is_expired(upload):
            upload["status"] = "expired"
            await self.repository.write(upload)
        return {"upload": self.public_upload(upload)}

    async def complete(self, upload_id: str, payload: CompleteUploadRequest) -> dict[str, Any]:
        lock = await self._completion_lock(upload_id)
        try:
            async with lock:
                return await self._complete_locked(upload_id, payload)
        finally:
            await self._release_completion_lock(upload_id, lock)

    async def _complete_locked(self, upload_id: str, payload: CompleteUploadRequest) -> dict[str, Any]:
        upload = await self.repository.read(upload_id)

        # Idempotency: already fully committed
        if upload.get("status") == "committed":
            session = await self.sessions.read(str(upload["sessionId"]))
            job = await self.jobs.repository.read(str(upload["jobId"])) if upload.get("jobId") else None
            return {
                "session": self.sessions.public_session(session),
                "upload": self.public_upload(upload),
                "job": self.jobs.public_job(job),
            }

        # Idempotency: assembly already running in background
        if upload.get("status") == "assembling":
            session = await self.sessions.read(str(upload["sessionId"]))
            job = await self.jobs.repository.read(str(upload["jobId"])) if upload.get("jobId") else None
            return {
                "session": self.sessions.public_session(session),
                "upload": self.public_upload(upload),
                "job": self.jobs.public_job(job),
            }

        self._assert_not_expired(upload)

        # Lightweight completeness check: verify all declared bytes are present before
        # committing.  The heavy SHA-256 + file-assembly work happens in the background.
        for file_record in upload.get("files") or []:
            expected_size = int(file_record.get("sizeBytes") or 0)
            parts = file_record.get("parts") or []
            uploaded_size = sum(int(p.get("sizeBytes") or 0) for p in parts)
            if not parts:
                raise AppError(f"{file_record.get('kind')} upload has no parts.", status_code=400)
            if uploaded_size != expected_size:
                raise AppError(
                    f"{file_record.get('kind')} upload is incomplete "
                    f"({uploaded_size}/{expected_size} bytes received, {len(parts)} parts).",
                    status_code=400,
                )

        should_process = upload.get("autoProcess")
        if payload.autoProcess is not None:
            should_process = payload.autoProcess

        # Mark as assembling immediately so duplicate requests are rejected and
        # the client knows the server has started work.
        upload["status"] = "assembling"
        upload["assemblingAt"] = utc_now_iso()
        await self.repository.write(upload)

        session = await self.sessions.read(str(upload["sessionId"]))
        session["status"] = "assembling"
        session["error"] = None
        session["upload"] = {
            "id": upload["id"],
            "status": "assembling",
            "strategy": upload.get("strategy"),
            "assemblingAt": upload["assemblingAt"],
        }
        await self.sessions.write(session)
        await self.events.publish(
            str(session["id"]),
            "status",
            {"code": "assembling", "message": "Assembling uploaded parts into final files..."},
        )

        # Fire-and-forget: heavy I/O (part concatenation, SHA-256, ffprobe) runs in
        # a background asyncio task so this HTTP handler returns in < 1 s. The task
        # is tracked in a registry to keep a strong reference (a bare
        # asyncio.create_task can be garbage-collected mid-run) and to surface
        # any unhandled exception.
        self._background_tasks.spawn(
            self._assemble_and_dispatch(upload_id, bool(should_process)),
            name=f"assemble-upload:{upload_id}",
        )

        job = await self.jobs.repository.read(str(upload["jobId"])) if upload.get("jobId") else None
        return {
            "session": self.sessions.public_session(session),
            "upload": self.public_upload(upload),
            "job": self.jobs.public_job(job),
        }

    async def _assemble_and_dispatch(self, upload_id: str, should_process: bool) -> None:
        """Background task: concatenate upload parts, validate, then start the processing job.

        Runs outside any HTTP request context so it must be self-contained and must
        handle its own exceptions — any unhandled error here would be silently swallowed
        by asyncio.create_task, so we catch broadly and emit an SSE error event instead.
        """
        session_id: str | None = None
        try:
            upload = await self.repository.read(upload_id)
            session_id = str(upload["sessionId"])

            committed_refs: dict[str, dict[str, Any]] = {}
            for file_record in upload.get("files") or []:
                storage_ref = await self.storage.complete_file(upload, file_record)
                committed_refs[str(file_record["kind"])] = storage_ref

            video_ref = committed_refs.get("video")
            case_study_ref = committed_refs.get("caseStudy")
            if not video_ref or not case_study_ref:
                raise AppError("Both video and caseStudy uploads must be completed.", status_code=400)

            await self._validate_committed_video(video_ref)
            self._validate_committed_case_study(case_study_ref)

            video_file_record = self._file_by_kind(upload.get("files") or [], "video")
            case_study_record = self._file_by_kind(upload.get("files") or [], "caseStudy")
            public_url = None
            if isinstance(self.storage, LocalObjectStorageService):
                public_url = self.storage.public_url_for_key(str(case_study_ref.get("key") or ""))
            (
                case_study_ref,
                case_study_asset,
                case_study_deduplicated,
            ) = await self.rubric_assets.register_case_study_storage_ref(
                storage_ref=case_study_ref,
                original_name=str(case_study_record.get("originalName") or ""),
                safe_name=str(case_study_record.get("safeName") or ""),
                public_url=public_url,
            )
            case_study_record["storageRef"] = case_study_ref
            await self.videos.save(
                session_id,
                video_ref,
                original_name=str(video_file_record.get("originalName") or ""),
                safe_name=str(video_file_record.get("safeName") or ""),
            )

            upload["status"] = "committed"
            upload["committedAt"] = utc_now_iso()

            session = await self.sessions.read(session_id)
            session["status"] = "uploaded"
            session["error"] = None
            session["upload"] = {
                "id": upload["id"],
                "status": "committed",
                "strategy": upload.get("strategy"),
                "committedAt": upload["committedAt"],
            }
            session["files"] = {
                "video": self._committed_file_meta(upload, "video", video_ref),
                "caseStudy": self._committed_file_meta(
                    upload,
                    "caseStudy",
                    case_study_ref,
                    case_study_asset,
                    case_study_deduplicated,
                ),
            }

            job = None
            if should_process:
                task_type = "auto_crop" if upload.get("workflow") == "long" else "process_session"
                session["status"] = "queued"
                job = await self.jobs.enqueue(
                    session_id,
                    task_type,
                    {"uploadId": upload_id, "workflow": upload.get("workflow")},
                    auto_start=False,
                )
                session["job"] = self.jobs.public_job(job)

            # Persist both records before touching the filesystem. If the process
            # crashes here, both records show their final state and startup
            # recovery has nothing to fix.  Any orphaned part files left by a
            # crash-before-abort are wasteful but safe and can be GC'd later.
            await self.repository.write(upload)
            await self.sessions.write(session)

            # Part files are only deleted once both records are durable.
            await self.storage.abort_upload(upload)

            await self.events.publish(
                session_id,
                "status",
                {"code": "upload_committed", "message": "Upload committed."},
            )
            if job:
                await self.jobs.start_job(job)

        except Exception as error:
            # Mark the upload itself failed — leaving it on "assembling" would
            # make every retry of /complete hit the idempotency branch and
            # return "assembling" forever, with no way to recover client-side.
            # A "failed" upload falls through the idempotency checks, so the
            # client can POST /complete again to retry assembly (part files are
            # only deleted after a successful commit).
            try:
                failed_upload = await self.repository.read(upload_id)
                failed_upload["status"] = "failed"
                failed_upload["error"] = str(error)
                failed_upload["failedAt"] = utc_now_iso()
                await self.repository.write(failed_upload)
            except Exception:
                logger.exception("Failed to persist failed upload state for upload %s.", upload_id)
            if session_id:
                try:
                    session = await self.sessions.read(session_id)
                    session["status"] = "failed"
                    session["error"] = str(error)
                    await self.sessions.write(session)
                    await self.events.publish(
                        session_id,
                        "status",
                        {"code": "failed", "message": f"Upload assembly failed: {error}"},
                    )
                except Exception:
                    pass

    async def abort(self, upload_id: str) -> dict[str, Any]:
        upload = await self.repository.read(upload_id)
        if upload.get("status") == "committed":
            raise AppError("Committed source uploads cannot be aborted.", status_code=409)
        await self.storage.abort_upload(upload)
        upload["status"] = "aborted"
        upload["abortedAt"] = utc_now_iso()
        await self.repository.write(upload)
        session = await self.sessions.read(str(upload["sessionId"]))
        session["status"] = "cancelled"
        session["error"] = "Upload aborted."
        await self.sessions.write(session)
        if upload.get("jobId"):
            await self.jobs.cancel(str(upload["jobId"]), "Upload aborted.")
        return {"upload": self.public_upload(upload), "session": self.sessions.public_session(session)}

    async def recover_stale_assembling_uploads(self) -> None:
        """Mark uploads and sessions stuck in ``"assembling"`` as failed at startup.

        Background assembly tasks are bound to the process lifetime — a server
        restart (including hot-reload) silently kills them, leaving records in
        ``"assembling"`` indefinitely.  This method runs two passes:

        Pass 1 — upload records in ``"assembling"``:
            Covers the common case where the assembly task was killed mid-run.
            The corresponding session is also transitioned to ``"failed"`` so
            the client receives a clear error rather than spinning forever.

        Pass 2 — session records still in ``"assembling"`` after pass 1:
            Covers the narrow crash window introduced by the new write-order
            (upload record persisted, session write did not complete).  The
            assembled files already exist on disk in this case; the session is
            still marked failed so the user is prompted to re-upload cleanly.

        Raw part files and any assembled output files are left on disk — cleanup
        is the caller's responsibility (explicit abort or a future GC pass).
        """
        _FAILURE_MESSAGE = (
            "Upload assembly was interrupted by a server restart. "
            "Please start a new assessment to re-upload."
        )
        recovered = 0

        # --- Pass 1: upload records stuck in "assembling" -------------------
        try:
            all_uploads = await self.repository.read_all()
        except Exception as exc:  # noqa: BLE001
            logger.error("Startup upload recovery: could not read upload records — %s", exc)
            return

        stale_uploads = [u for u in all_uploads if u.get("status") == "assembling"]

        for upload in stale_uploads:
            upload_id = str(upload.get("id", ""))
            session_id = str(upload.get("sessionId", ""))

            try:
                upload["status"] = "failed"
                upload["failedAt"] = utc_now_iso()
                await self.repository.write(upload)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Startup upload recovery: could not mark upload %s as failed — %s",
                    upload_id,
                    exc,
                )
                continue

            if not session_id:
                continue

            try:
                session = await self.sessions.read(session_id)
                if session.get("status") == "assembling":
                    session["status"] = "failed"
                    session["error"] = _FAILURE_MESSAGE
                    await self.sessions.write(session)
                    recovered += 1
                    logger.warning(
                        "Startup upload recovery: upload %s (session %s) marked failed.",
                        upload_id,
                        session_id,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Startup upload recovery: could not update session %s — %s",
                    session_id,
                    exc,
                )

        # --- Pass 2: sessions still "assembling" after pass 1 ---------------
        # Handles the window where the upload record was committed but the
        # session write had not yet completed before the process was killed.
        try:
            all_sessions = await self.sessions.list_sessions()
        except Exception as exc:  # noqa: BLE001
            logger.error("Startup upload recovery: could not read session records — %s", exc)
            return

        for session in all_sessions:
            if session.get("status") != "assembling":
                continue
            try:
                session["status"] = "failed"
                session["error"] = _FAILURE_MESSAGE
                await self.sessions.write(session)
                recovered += 1
                logger.warning(
                    "Startup upload recovery: orphaned assembling session %s marked failed.",
                    session.get("id"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Startup upload recovery: could not update orphaned session %s — %s",
                    session.get("id"),
                    exc,
                )

        if recovered:
            logger.warning(
                "Startup upload recovery complete: %d record(s) transitioned to 'failed'. "
                "Affected users must re-upload.",
                recovered,
            )

    # Pre-commit upload states whose session/job may still be safely failed by
    # the expiry sweep. A session past these (uploaded/queued/processing/…) has
    # already left the upload phase and must never be clobbered by cleanup.
    _RECOVERABLE_UPLOAD_STATES = frozenset({"initiated", "uploading", "failed"})
    _RECOVERABLE_SESSION_STATES = frozenset({"waiting_for_upload", "uploading", "assembling"})

    async def recover_expired_uploads(self) -> None:
        """Reclaim uploads abandoned mid-transfer once their TTL has elapsed.

        The ``assembling`` sweep (``recover_stale_assembling_uploads``) only covers
        uploads killed during background assembly. An upload interrupted *earlier*
        — during part transfer — is left in ``initiated``/``uploading`` with its
        raw part files on disk and its session pinned at ``waiting_for_upload``.
        Nothing else deletes those parts, so a 500 MB video interrupted by, say, a
        transient ``os.replace`` failure (now retried, but any client disconnect
        does the same) leaks its bytes indefinitely and leaves a dead session card.

        TTL (``expiresAt``, default 24h) is the safety signal: an upload still
        inside its window may be a live transfer, so only *expired* records are
        reclaimed. For each: mark it ``expired``, delete its part tree via
        ``abort_upload``, fail the still-pre-commit session, and cancel the pending
        job. A ``committed`` upload's session/job are already past the upload phase
        and are left untouched. All steps are idempotent, so repeated startups and
        a later explicit abort cannot double-act or corrupt state.
        """
        try:
            all_uploads = await self.repository.read_all()
        except Exception as exc:  # noqa: BLE001
            logger.error("Expired-upload sweep: could not read upload records — %s", exc)
            return

        reclaimed = 0
        for upload in all_uploads:
            if str(upload.get("status")) not in self._RECOVERABLE_UPLOAD_STATES:
                continue
            if not self._is_expired(upload):
                continue

            upload_id = str(upload.get("id", ""))
            session_id = str(upload.get("sessionId", ""))

            # 1. Delete raw part files first — reclaiming bytes is the primary goal
            #    and must not be blocked by a later record/session write failing.
            try:
                await self.storage.abort_upload(upload)
            except Exception as exc:  # noqa: BLE001
                logger.error("Expired-upload sweep: could not delete parts for %s — %s", upload_id, exc)

            # 2. Mark the upload record terminal so the sweep never revisits it.
            try:
                upload["status"] = "expired"
                upload["expiredAt"] = utc_now_iso()
                await self.repository.write(upload)
            except Exception as exc:  # noqa: BLE001
                logger.error("Expired-upload sweep: could not mark upload %s expired — %s", upload_id, exc)
                continue

            # 3. Fail the session, but only while it is still in the upload phase.
            if session_id:
                try:
                    session = await self.sessions.read(session_id)
                    if str(session.get("status")) in self._RECOVERABLE_SESSION_STATES:
                        session["status"] = "failed"
                        session["error"] = (
                            "Upload expired before completion. Please start a new assessment to re-upload."
                        )
                        await self.sessions.write(session)
                except FileNotFoundError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    logger.error("Expired-upload sweep: could not fail session %s — %s", session_id, exc)

            # 4. Cancel the pending job (no-ops if already terminal).
            job_id = str(upload.get("jobId") or "")
            if job_id:
                try:
                    await self.jobs.cancel(job_id, "Upload expired before completion.")
                except Exception as exc:  # noqa: BLE001
                    logger.error("Expired-upload sweep: could not cancel job %s — %s", job_id, exc)

            reclaimed += 1
            logger.warning(
                "Expired-upload sweep: reclaimed upload %s (session %s) — parts deleted, session failed.",
                upload_id,
                session_id,
            )

        if reclaimed:
            logger.warning("Expired-upload sweep complete: %d abandoned upload(s) reclaimed.", reclaimed)

    def public_upload(self, upload: dict[str, Any]) -> dict[str, Any]:
        files = []
        for item in upload.get("files") or []:
            files.append(
                {
                    "fileId": item.get("fileId"),
                    "kind": item.get("kind"),
                    "originalName": item.get("originalName"),
                    "mimeType": item.get("mimeType"),
                    "sizeBytes": item.get("sizeBytes"),
                    "uploadedBytes": item.get("uploadedBytes") or 0,
                    "status": item.get("status"),
                    "parts": [
                        {
                            "partNumber": part.get("partNumber"),
                            "sizeBytes": part.get("sizeBytes"),
                            "sha256": part.get("sha256"),
                        }
                        for part in item.get("parts") or []
                    ],
                }
            )
        return {
            "id": upload.get("id"),
            "sessionId": upload.get("sessionId"),
            "workflow": upload.get("workflow"),
            "status": upload.get("status"),
            "strategy": upload.get("strategy"),
            "provider": upload.get("provider"),
            "createdAt": upload.get("createdAt"),
            "expiresAt": upload.get("expiresAt"),
            "committedAt": upload.get("committedAt"),
            "files": files,
            "jobId": upload.get("jobId"),
        }

    async def _sync_pending_session_upload(self, upload: dict[str, Any]) -> None:
        session = await self.sessions.read(str(upload["sessionId"]))
        session["upload"] = {
            "id": upload["id"],
            "status": upload.get("status"),
            "strategy": upload.get("strategy"),
            "expiresAt": upload.get("expiresAt"),
        }
        session["files"] = {
            "video": self._pending_file_meta(upload.get("files") or [], "video"),
            "caseStudy": self._pending_file_meta(upload.get("files") or [], "caseStudy"),
        }
        await self.sessions.write(session)

    def _validate_declared_files(self, payload: InitiateUploadRequest) -> None:
        for item in payload.files:
            extension = Path(item.originalName).suffix.lower()
            if item.kind == "video":
                if item.sizeBytes > self.settings.max_video_upload_bytes:
                    raise AppError(f"Video exceeds the {self.settings.max_video_upload_mb} MB limit.", status_code=413)
                if extension not in VIDEO_EXTENSIONS:
                    raise AppError("Video file must be MP4, MOV, MKV, WEBM, M4V, or AVI.", status_code=400)
                if item.mimeType and not str(item.mimeType).startswith("video/"):
                    raise AppError("Video MIME type must start with video/.", status_code=400)
            elif item.kind == "caseStudy":
                if extension != ".pdf":
                    raise AppError("caseStudy must be a PDF file.", status_code=400)
                if item.mimeType and item.mimeType != "application/pdf":
                    raise AppError("caseStudy MIME type must be application/pdf.", status_code=400)

    async def _validate_committed_video(self, storage_ref: dict[str, Any]) -> None:
        local_path = storage_ref.get("localPath")
        if not local_path:
            raise AppError("Committed video is not available to the local worker.", status_code=500)
        try:
            await self.media.get_video_duration_seconds(Path(str(local_path)))
        except RuntimeError as error:
            message = str(error) or "Video validation failed."
            if "was not found in PATH" in message:
                raise AppError(message, status_code=500) from error
            raise AppError(
                "Uploaded video could not be read. Confirm the file is a valid video with readable metadata.",
                status_code=400,
            ) from error

    @staticmethod
    def _validate_committed_case_study(storage_ref: dict[str, Any]) -> None:
        local_path = storage_ref.get("localPath")
        path = Path(str(local_path or ""))
        if not local_path or path.suffix.lower() != ".pdf" or not path.exists():
            raise AppError("Committed caseStudy is not a PDF file.", status_code=400)

    def _committed_file_meta(
        self,
        upload: dict[str, Any],
        kind: str,
        storage_ref: dict[str, Any],
        rubric_asset: dict[str, Any] | None = None,
        rubric_deduplicated: bool = False,
    ) -> dict[str, Any]:
        file_record = self._file_by_kind(upload.get("files") or [], kind)
        meta = {
            "originalName": file_record.get("originalName"),
            "fileName": file_record.get("safeName"),
            "absolutePath": storage_ref.get("localPath"),
            "sizeBytes": storage_ref.get("sizeBytes"),
            "mimeType": storage_ref.get("mimeType"),
            "storageRef": storage_ref,
        }
        if kind == "video":
            if isinstance(self.storage, LocalObjectStorageService):
                meta["url"] = self.storage.public_url_for_key(str(storage_ref.get("key") or ""))
        if rubric_asset:
            meta["rubricAssetId"] = rubric_asset["id"]
            meta["rubricDeduplicated"] = rubric_deduplicated
            meta["contentSha256"] = rubric_asset["contentSha256"]
            if rubric_asset.get("publicUrl"):
                meta["url"] = rubric_asset["publicUrl"]
        return meta

    @staticmethod
    def _pending_file_meta(files: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
        file_record = AsyncUploadService._file_by_kind(files, kind)
        if not file_record:
            return None
        return {
            "originalName": file_record.get("originalName"),
            "fileName": file_record.get("safeName"),
            "sizeBytes": file_record.get("sizeBytes"),
            "mimeType": file_record.get("mimeType"),
            "uploadStatus": file_record.get("status"),
            "uploadedBytes": file_record.get("uploadedBytes") or 0,
        }

    @staticmethod
    def _file_by_kind(files: list[dict[str, Any]], kind: str) -> dict[str, Any]:
        for file_record in files:
            if str(file_record.get("kind")) == kind:
                return file_record
        return {}

    @staticmethod
    def _first_file_id(upload: dict[str, Any]) -> str:
        files = upload.get("files") or []
        if not files:
            raise AppError("Upload has no files.", status_code=404)
        return str(files[0]["fileId"])

    def _expires_at(self) -> str:
        expires = datetime.now(timezone.utc) + timedelta(hours=max(1, self.settings.upload_session_ttl_hours))
        return expires.isoformat().replace("+00:00", "Z")

    def _is_expired(self, upload: dict[str, Any]) -> bool:
        if upload.get("status") in {"committed", "aborted", "expired"}:
            return False
        try:
            expires = datetime.fromisoformat(str(upload.get("expiresAt")).replace("Z", "+00:00"))
            return datetime.now(timezone.utc) > expires
        except Exception:
            return False

    def _assert_not_expired(self, upload: dict[str, Any]) -> None:
        if self._is_expired(upload):
            upload["status"] = "expired"
            raise AppError("Upload session has expired.", status_code=410)

    async def _completion_lock(self, upload_id: str) -> asyncio.Lock:
        async with self._completion_locks_guard:
            lock = self._completion_locks.get(upload_id)
            if lock is None:
                lock = asyncio.Lock()
                self._completion_locks[upload_id] = lock
            return lock

    async def _release_completion_lock(self, upload_id: str, lock: asyncio.Lock) -> None:
        async with self._completion_locks_guard:
            if not lock.locked() and self._completion_locks.get(upload_id) is lock:
                self._completion_locks.pop(upload_id, None)
