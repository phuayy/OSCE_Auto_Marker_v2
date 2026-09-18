from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.locks import KeyedLocks
from app.core.tasks import BackgroundTaskRegistry
from app.core.utils import parse_iso, utc_now_iso
from app.domain.actors import PROVENANCE_KEY, Actor
from app.domain.enums import TaskType, UploadStatus, Workflow
from app.domain.session_lifecycle import fail_session
from app.domain.sessions import SessionStatus, empty_outputs
from app.pipeline.media import MediaPipeline
from app.repositories.corpus_repository import CorpusRepository
from app.repositories.upload_repository import UploadRepository
from app.repositories.video_repository import VideoRepository
from app.schemas.uploads import CompleteUploadRequest, InitiateUploadRequest
from app.services.event_service import EventService
from app.services.job_queue_service import JobQueueService
from app.services.rubric_asset_service import RubricAssetService
from app.services.session_service import SessionService
from app.storage import ObjectStorage

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}

# How often, at most, a running transfer's byte count is mirrored onto the
# session row. The browser renders upload progress from its own tracker; the
# server copy exists for other tabs and for the record. Mirroring every part
# made a 2 GB upload 256 whole-document session writes, each evicting the
# session-index cache. The mirror always lands when a file finishes.
SESSION_UPLOAD_MIRROR_INTERVAL_SECONDS = 5.0


class AsyncUploadService:
    def __init__(
        self,
        settings: Settings,
        repository: UploadRepository,
        sessions: SessionService,
        storage: ObjectStorage,
        jobs: JobQueueService,
        media: MediaPipeline,
        events: EventService,
        rubric_assets: RubricAssetService,
        videos: VideoRepository,
        corpora: CorpusRepository | None = None,
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
        self.corpora = corpora
        # One lock per upload id. The upload record is a JSON file rewritten on
        # every part, so two parts of the same upload must never interleave
        # their read → mutate → write; different uploads stay independent.
        self._upload_locks = KeyedLocks()
        self._last_session_mirror: dict[str, float] = {}
        # Holds strong references to background assembly tasks so the event loop
        # cannot garbage-collect them mid-execution (see BackgroundTaskRegistry).
        self._background_tasks = BackgroundTaskRegistry()

    async def initiate(self, payload: InitiateUploadRequest, *, actor: Actor | None = None) -> dict[str, Any]:
        if actor is not None:
            async with self.repository.admission(actor.user_id) as active_uploads:
                limit = max(1, self.settings.max_concurrent_uploads_per_user)
                if active_uploads >= limit:
                    raise AppError(
                        f"You already have {limit} uploads in flight. Complete or abort an upload before starting another.",
                        status_code=429,
                    )
                return await self._initiate(payload, actor=actor)
        async with self.repository.database.unit_of_work():
            return await self._initiate(payload, actor=actor)

    async def _initiate(self, payload: InitiateUploadRequest, *, actor: Actor | None = None) -> dict[str, Any]:
        """Reserve a session and its job for a chunked upload.

        ``actor`` is the account making the request; the session records it as
        its creator (a snapshot, so a later rename or deletion of the account
        does not rewrite history). None only for a caller with no session,
        which the API never allows here — it is optional so the service stays
        usable from tests and scripts.
        """
        self._validate_declared_files(payload)
        corpus_snapshot = await self._resolve_corpus_snapshot(payload.corpusId)
        session_id = str(uuid4())
        upload_id = str(uuid4())
        expires_at = self._expires_at()

        prepared_files = []
        upload_files = []
        for item in payload.files:
            file_id = str(uuid4())
            prepared = await self.storage.prepare_upload_file(
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
                    "status": UploadStatus.INITIATED,
                    "createdAt": utc_now_iso(),
                }
            )

        task_type = TaskType.AUTO_CROP if payload.workflow == Workflow.LONG else TaskType.PROCESS_SESSION
        job = await self.jobs.create_waiting_job(
            session_id,
            task_type,
            payload={"workflow": payload.workflow, "autoProcess": payload.autoProcess, "uploadId": upload_id},
        )
        session = {
            "id": session_id,
            "name": payload.sessionName or "",
            "createdAt": utc_now_iso(),
            # Who uploaded it, as they were at the time.
            PROVENANCE_KEY: actor.to_provenance() if actor is not None else None,
            "status": SessionStatus.WAITING_FOR_UPLOAD,
            "workflow": payload.workflow,
            # Long-workflow auto-crop method ("bells" | "person"); None defers
            # to the server default at job execution time.
            "segmentation": payload.segmentation if payload.workflow == Workflow.LONG else None,
            # Resolved occupancy rule for the person detector (preset name plus
            # the numbers it meant at upload time), or None to use its default.
            "segmentationOptions": payload.resolved_segmentation_options(),
            # Resolved horizontal region-of-interest for the person detector
            # (preset-independent), or None to use the whole frame.
            "regionFocusOptions": payload.resolved_region_focus_options(),
            # Snapshot of the chosen transcription corpus (or None); inherited
            # by clip children so one pick covers every clip in the session.
            "corpus": corpus_snapshot,
            "upload": {
                "id": upload_id,
                "status": UploadStatus.INITIATED,
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
            "outputs": empty_outputs(),
            "error": None,
        }
        upload = {
            "id": upload_id,
            "sessionId": session_id,
            # Same snapshot as the session's (see PROVENANCE_KEY above), stamped
            # here too so the per-chunk ownership guard on PUT /parts/{n} reads
            # it off the upload record it already loads, rather than paying a
            # second lookup (the session) on every chunk of a large transfer.
            PROVENANCE_KEY: actor.to_provenance() if actor is not None else None,
            "workflow": payload.workflow,
            "autoProcess": payload.autoProcess,
            "status": UploadStatus.INITIATED,
            "strategy": self.storage.strategy,
            "provider": self.storage.provider,
            "createdAt": utc_now_iso(),
            "expiresAt": expires_at,
            "files": upload_files,
            "jobId": job["id"],
        }
        await self.sessions.create_named(session)
        await self.repository.write(upload)
        await self.events.publish(session_id, "status", {"code": UploadStatus.UPLOADING, "message": "Upload session created."})
        return {
            "session": self.sessions.public_session(session),
            "uploadId": upload_id,
            "strategy": self.storage.strategy,
            "partSizeBytes": self.settings.upload_part_size_bytes,
            "expiresAt": expires_at,
            "fileUploads": [prepared.to_response() for prepared in prepared_files],
            "job": self.jobs.public_job(job),
        }

    async def _resolve_corpus_snapshot(self, corpus_id: str | None) -> dict[str, Any] | None:
        if not corpus_id:
            return None
        if self.corpora is None:
            raise AppError("Transcription corpora are not configured.", status_code=400)
        try:
            return await self.corpora.snapshot(corpus_id)
        except LookupError as error:
            raise AppError("Transcription corpus not found.", status_code=400) from error

    async def put_part(self, upload_id: str, file_id: str | None, part_number: int, body: bytes) -> dict[str, Any]:
        """Store one part and record it on the upload.

        Held under the upload's lock for the whole read → store → write, so parts
        arriving in parallel (a browser with several chunk workers, or a retry
        overlapping the request it is retrying) each see the previous part's
        record. Without the lock the last writer's record wins and the other
        parts' bytes sit on disk unrecorded until ``complete`` rejects the whole
        upload as incomplete.
        """
        async with self._upload_locks.hold(upload_id), self.repository.locked(upload_id):
            upload = await self.repository.read(upload_id)
            self._assert_not_expired(upload)
            resolved_file_id = file_id or self._first_file_id(upload)
            part = await self.storage.put_part(upload, resolved_file_id, part_number, body)
            await self.repository.write(upload)
            await self._sync_pending_session_upload(upload, file_id=resolved_file_id)
        await self.events.publish(
            str(upload["sessionId"]),
            "status",
            {
                "code": UploadStatus.UPLOADING,
                "message": f"Uploaded part {part_number}.",
                "uploadId": upload_id,
                "fileId": resolved_file_id,
            },
        )
        return {"part": part, "upload": self.public_upload(upload)}

    async def status(self, upload_id: str) -> dict[str, Any]:
        upload = await self.repository.read(upload_id)
        if self._is_expired(upload) and upload.get("status") in self._RECOVERABLE_UPLOAD_STATES:
            upload["status"] = UploadStatus.EXPIRED
        return {"upload": self.public_upload(upload)}

    async def complete(self, upload_id: str, payload: CompleteUploadRequest) -> dict[str, Any]:
        # Same lock as ``put_part``: a straggling part cannot land between the
        # completeness check and the flip to "assembling".
        async with self._upload_locks.hold(upload_id), self.repository.locked(upload_id):
            previous = await self.repository.read(upload_id)
            result = await self._complete_locked(upload_id, payload)
        if previous.get("status") not in {UploadStatus.ASSEMBLING, UploadStatus.COMMITTED}:
            self._background_tasks.spawn(
                self._assemble_and_dispatch(upload_id, bool(payload.autoProcess if payload.autoProcess is not None else previous.get("autoProcess"))),
                name=f"assemble-upload:{upload_id}",
            )
        return result

    async def _complete_locked(self, upload_id: str, payload: CompleteUploadRequest) -> dict[str, Any]:
        upload = await self.repository.read(upload_id)

        # Idempotency: already fully committed
        if upload.get("status") == UploadStatus.COMMITTED:
            session = await self.sessions.read(str(upload["sessionId"]))
            job = await self.jobs.repository.read(str(upload["jobId"])) if upload.get("jobId") else None
            return {
                "session": self.sessions.public_session(session),
                "upload": self.public_upload(upload),
                "job": self.jobs.public_job(job),
            }

        # Idempotency: assembly already running in background
        if upload.get("status") == UploadStatus.ASSEMBLING:
            session = await self.sessions.read(str(upload["sessionId"]))
            job = await self.jobs.repository.read(str(upload["jobId"])) if upload.get("jobId") else None
            return {
                "session": self.sessions.public_session(session),
                "upload": self.public_upload(upload),
                "job": self.jobs.public_job(job),
            }

        self._assert_not_expired(upload)
        if upload.get("status") not in {UploadStatus.INITIATED, UploadStatus.UPLOADING, UploadStatus.FAILED}:
            raise AppError("Upload cannot be completed in its current state.", status_code=409)

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
        # the client knows the server has started work. The resolved autoProcess
        # is recorded too, so a restart can resume assembly with the same answer.
        upload["status"] = UploadStatus.ASSEMBLING
        upload["assemblingAt"] = utc_now_iso()
        upload["autoProcess"] = bool(should_process)
        await self.repository.write(upload)

        upload_ref = {
            "id": upload["id"],
            "status": UploadStatus.ASSEMBLING,
            "strategy": upload.get("strategy"),
            "assemblingAt": upload["assemblingAt"],
        }

        def to_assembling(current: dict[str, Any]) -> Any:
            current["status"] = SessionStatus.ASSEMBLING
            current["error"] = None
            current["upload"] = upload_ref
            return None

        session = await self.sessions.update(str(upload["sessionId"]), to_assembling)
        await self.events.publish(
            str(session["id"]),
            "status",
            {"code": UploadStatus.ASSEMBLING, "message": "Assembling uploaded parts into final files..."},
        )

        # Fire-and-forget: heavy I/O (part concatenation, SHA-256, ffprobe) runs in
        # a background asyncio task so this HTTP handler returns in < 1 s. The task
        # is tracked in a registry to keep a strong reference (a bare
        # asyncio.create_task can be garbage-collected mid-run) and to surface
        # any unhandled exception.
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
            await self._validate_committed_case_study(case_study_ref)

            case_study_record = self._file_by_kind(upload.get("files") or [], "caseStudy")
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
            job = await self._commit_assembled(
                upload, should_process, video_ref, case_study_ref, case_study_asset, case_study_deduplicated,
            )

            # Part files are only deleted once both records are durable.
            try:
                await self.storage.abort_upload(upload)
            except OSError:
                logger.warning("Committed upload parts need cleanup: %s", upload_id, exc_info=True)
            await self.events.publish(session_id, "status", {"code": "upload_committed", "message": "Upload committed."})
            if job:
                await self.jobs.start_job(job)

        except Exception as error:
            await self._assembly_failed(upload_id, session_id, error)

    async def _commit_assembled(
        self, upload: dict[str, Any], should_process: bool,
        video_ref: dict[str, Any], case_study_ref: dict[str, Any],
        case_study_asset: dict[str, Any] | None, case_study_deduplicated: bool,
    ) -> dict[str, Any] | None:
        upload_id = str(upload["id"])
        session_id = str(upload["sessionId"])
        async with self.repository.locked(upload_id):
            current = await self.repository.read(upload_id)
            if current.get("status") != UploadStatus.ASSEMBLING:
                raise AppError("Upload is no longer assembling.", status_code=409)
            current["files"] = upload["files"]
            upload = current
            video_file_record = self._file_by_kind(upload.get("files") or [], "video")
            await self.videos.save(
                session_id, video_ref,
                original_name=str(video_file_record.get("originalName") or ""),
                safe_name=str(video_file_record.get("safeName") or ""),
            )

            upload["status"] = UploadStatus.COMMITTED
            upload["committedAt"] = utc_now_iso()

            upload_ref = {
                "id": upload["id"],
                "status": UploadStatus.COMMITTED,
                "strategy": upload.get("strategy"),
                "committedAt": upload["committedAt"],
            }
            committed_files = {
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
                task_type = TaskType.AUTO_CROP if upload.get("workflow") == Workflow.LONG else TaskType.PROCESS_SESSION
                job = await self.jobs.enqueue(
                    session_id,
                    task_type,
                    {"uploadId": upload_id, "workflow": upload.get("workflow")},
                    auto_start=False,
                )
            public_job = self.jobs.public_job(job) if job else None
            next_status = SessionStatus.QUEUED if job else SessionStatus.UPLOADED

            def commit_session(current: dict[str, Any]) -> Any:
                current["status"] = next_status
                current["error"] = None
                current["upload"] = upload_ref
                current["files"] = committed_files
                if public_job is not None:
                    current["job"] = public_job
                return None

            # Persist both records before touching the filesystem. If the process
            # crashes here, both records show their final state and startup
            # recovery has nothing to fix.  Any orphaned part files left by a
            # crash-before-abort are wasteful but safe and can be GC'd later.
            await self.repository.write(upload)
            await self.sessions.update(session_id, commit_session)

            # Part files are only deleted once both records are durable.
            return job

    async def _assembly_failed(self, upload_id: str, session_id: str | None, error: Exception) -> None:
        logger.error("Upload assembly failed: %s", upload_id, exc_info=error)
        async with self.repository.locked(upload_id):
            current = await self.repository.read(upload_id)
            if current.get("status") != UploadStatus.ASSEMBLING:
                logger.warning("Post-assembly operation failed for %s", upload_id, exc_info=error)
                return
            # Mark the upload itself failed — leaving it on "assembling" would
            # make every retry of /complete hit the idempotency branch and
            # return "assembling" forever, with no way to recover client-side.
            # A "failed" upload falls through the idempotency checks, so the
            # client can POST /complete again to retry assembly (part files are
            # only deleted after a successful commit).
            try:
                failed_upload = await self.repository.read(upload_id)
                failed_upload["status"] = UploadStatus.FAILED
                failed_upload["error"] = str(error)
                failed_upload["failedAt"] = utc_now_iso()
                await self.repository.write(failed_upload)
            except Exception:
                logger.exception("Failed to persist failed upload state for upload %s.", upload_id)
                raise
            if session_id:
                try:
                    await self.sessions.update(session_id, self._fail_session_mutator(str(error)))
                    await self.events.publish(
                        session_id,
                        "status",
                        {"code": UploadStatus.FAILED, "message": f"Upload assembly failed: {error}"},
                    )
                except Exception:
                    logger.exception("Failed to persist failed session state for upload %s.", upload_id)
                    raise

    async def abort(self, upload_id: str) -> dict[str, Any]:
        async with self._upload_locks.hold(upload_id), self.repository.locked(upload_id):
            return await self._abort_locked(upload_id)

    async def _abort_locked(self, upload_id: str) -> dict[str, Any]:
        upload = await self.repository.read(upload_id)
        if upload.get("status") in {UploadStatus.COMMITTED, UploadStatus.ASSEMBLING}:
            raise AppError("Committed source uploads cannot be aborted.", status_code=409)
        await self.storage.abort_upload(upload)
        upload["status"] = UploadStatus.ABORTED
        upload["abortedAt"] = utc_now_iso()
        await self.repository.write(upload)

        def cancel(current: dict[str, Any]) -> Any:
            current["status"] = SessionStatus.CANCELLED
            current["error"] = "Upload aborted."
            return None

        session = await self.sessions.update(str(upload["sessionId"]), cancel)
        if upload.get("jobId"):
            await self.jobs.cancel(str(upload["jobId"]), "Upload aborted.")
        return {"upload": self.public_upload(upload), "session": self.sessions.public_session(session)}

    async def recover_stale_assembling_uploads(self) -> None:
        """Resume — or, when nothing is left to resume, fail — work stuck in ``"assembling"``.

        Background assembly tasks are bound to the process lifetime: a server
        restart (including hot-reload) silently kills them, leaving the upload
        record and the session in ``"assembling"`` indefinitely.

        Pass 1 — upload records in ``"assembling"``:
            The parts are still on disk (they are only deleted after a commit),
            and ``_assemble_and_dispatch`` is idempotent by construction (temp
            file + rename, then records). So the right recovery is to run it
            again, not to make the user re-send the file. It is only failed when
            the parts no longer add up to the declared size.

        Pass 2 — session records still in ``"assembling"`` with no upload record
        in that state:
            The narrow window where the upload record was committed but the
            session write did not complete. Failed through ``update`` so only
            the status and error move; the rest of the document is kept.
        """
        _FAILURE_MESSAGE = (
            "Upload assembly was interrupted by a server restart and its parts are incomplete. "
            "Please start a new assessment to re-upload."
        )
        resumed = 0
        failed = 0

        # --- Pass 1: upload records stuck in "assembling" -------------------
        try:
            all_uploads = await self.repository.read_all()
        except Exception as exc:  # noqa: BLE001
            logger.error("Startup upload recovery: could not read upload records — %s", exc)
            return

        stale_uploads = [u for u in all_uploads if u.get("status") == UploadStatus.ASSEMBLING]
        resumed_sessions: set[str] = set()

        for upload in stale_uploads:
            upload_id = str(upload.get("id", ""))
            session_id = str(upload.get("sessionId", ""))

            if self._parts_complete(upload):
                self._background_tasks.spawn(
                    self._assemble_and_dispatch(upload_id, bool(upload.get("autoProcess"))),
                    name=f"assemble-upload:{upload_id}",
                )
                resumed += 1
                resumed_sessions.add(session_id)
                logger.warning(
                    "Startup upload recovery: upload %s (session %s) was mid-assembly; resuming assembly.",
                    upload_id,
                    session_id,
                )
                continue

            try:
                upload["status"] = UploadStatus.FAILED
                upload["failedAt"] = utc_now_iso()
                upload["error"] = _FAILURE_MESSAGE
                await self.repository.write(upload)
            except Exception as exc:  # noqa: BLE001
                logger.error("Startup upload recovery: could not mark upload %s as failed — %s", upload_id, exc)
                continue

            if not session_id:
                continue
            try:
                await self.sessions.update(session_id, self._fail_if_assembling(_FAILURE_MESSAGE))
                failed += 1
                logger.warning(
                    "Startup upload recovery: upload %s (session %s) had incomplete parts; marked failed.",
                    upload_id,
                    session_id,
                )
            except FileNotFoundError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.error("Startup upload recovery: could not update session %s — %s", session_id, exc)

        # --- Pass 2: sessions still "assembling" with no upload to resume -----
        try:
            all_sessions = await self.sessions.list_sessions()
        except Exception as exc:  # noqa: BLE001
            logger.error("Startup upload recovery: could not read session records — %s", exc)
            return

        for entry in all_sessions:
            if entry.get("status") != UploadStatus.ASSEMBLING:
                continue
            session_id = str(entry.get("id") or "")
            if not session_id or session_id in resumed_sessions:
                continue
            try:
                # ``entry`` is a list projection, not a document: it must never be
                # written back. ``update`` re-reads the real row and patches it.
                await self.sessions.update(session_id, self._fail_if_assembling(_FAILURE_MESSAGE))
                failed += 1
                logger.warning(
                    "Startup upload recovery: orphaned assembling session %s marked failed.",
                    session_id,
                )
            except FileNotFoundError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.error("Startup upload recovery: could not update orphaned session %s — %s", session_id, exc)

        if resumed or failed:
            logger.warning(
                "Startup upload recovery complete: %d assembly(ies) resumed, %d record(s) failed.",
                resumed,
                failed,
            )

    @staticmethod
    def _parts_complete(upload: dict[str, Any]) -> bool:
        """Every declared file has parts summing to its declared size."""
        files = upload.get("files") or []
        if not files:
            return False
        for file_record in files:
            expected = int(file_record.get("sizeBytes") or 0)
            received = sum(int(part.get("sizeBytes") or 0) for part in file_record.get("parts") or [])
            if not file_record.get("parts") or received != expected:
                return False
        return True

    @staticmethod
    def _fail_if_assembling(message: str):
        def mutate(session: dict[str, Any]) -> Any:
            return fail_session(session, message, expected_status=SessionStatus.ASSEMBLING)

        return mutate

    @staticmethod
    def _fail_session_mutator(message: str):
        def mutate(session: dict[str, Any]) -> Any:
            return fail_session(session, message)

        return mutate

    # Pre-commit upload states whose session/job may still be safely failed by
    # the expiry sweep. A session past these (uploaded/queued/processing/…) has
    # already left the upload phase and must never be clobbered by cleanup.
    _RECOVERABLE_UPLOAD_STATES = frozenset({UploadStatus.INITIATED, UploadStatus.UPLOADING, UploadStatus.FAILED})
    _RECOVERABLE_SESSION_STATES = frozenset({"waiting_for_upload", UploadStatus.UPLOADING, UploadStatus.ASSEMBLING})

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
            all_uploads = await self.repository.read_all(expired=True)
        except Exception as exc:  # noqa: BLE001
            logger.error("Expired-upload sweep: could not read upload records — %s", exc)
            return

        reclaimed = 0
        for upload in all_uploads:
            try:
                reclaimed += await self._reclaim_expired_upload(str(upload["id"]))
            except FileNotFoundError:
                continue
            except Exception:
                logger.warning("Expired upload cleanup will be retried: %s", upload["id"], exc_info=True)
        if reclaimed:
            logger.warning("Expired-upload sweep complete: %d abandoned upload(s) reclaimed.", reclaimed)

    async def _reclaim_expired_upload(self, upload_id: str) -> int:
        async with self.repository.locked(upload_id):
            upload = await self.repository.read(upload_id)
            expires = parse_iso(upload.get("expiresAt"))
            if expires is None or expires >= datetime.now(timezone.utc):
                return 0
            if upload.get("status") in {UploadStatus.COMMITTED, UploadStatus.ABORTED, UploadStatus.EXPIRED}:
                await self.storage.abort_upload(upload)
                await self.repository.delete_expired(upload_id)
                return 1
            if str(upload.get("status")) not in self._RECOVERABLE_UPLOAD_STATES:
                return 0
            session_id = str(upload.get("sessionId", ""))

            # 1. Delete raw part files first — reclaiming bytes is the primary goal
            #    and must not be blocked by a later record/session write failing.
            try:
                await self.storage.abort_upload(upload)
            except Exception as exc:  # noqa: BLE001
                logger.error("Expired-upload sweep: could not delete parts for %s — %s", upload_id, exc)
                raise

            # 2. Mark the upload record terminal so the sweep never revisits it.
            try:
                upload["status"] = UploadStatus.EXPIRED
                upload["expiredAt"] = utc_now_iso()
                await self.repository.write(upload)
            except Exception as exc:  # noqa: BLE001
                logger.error("Expired-upload sweep: could not mark upload %s expired — %s", upload_id, exc)
                raise

            # 3. Fail the session, but only while it is still in the upload phase.
            if session_id:
                def expire(current: dict[str, Any]) -> Any:
                    if str(current.get("status")) not in self._RECOVERABLE_SESSION_STATES:
                        return False
                    current["status"] = SessionStatus.FAILED
                    current["error"] = "Upload expired before completion. Please start a new assessment to re-upload."
                    return None

                try:
                    await self.sessions.update(session_id, expire)
                except FileNotFoundError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    logger.error("Expired-upload sweep: could not fail session %s — %s", session_id, exc)
                    raise

            # 4. Cancel the pending job (no-ops if already terminal).
            job_id = str(upload.get("jobId") or "")
            if job_id:
                try:
                    await self.jobs.cancel(job_id, "Upload expired before completion.")
                except Exception as exc:  # noqa: BLE001
                    logger.error("Expired-upload sweep: could not cancel job %s — %s", job_id, exc)
                    raise

            await self.repository.delete_expired(upload_id)
            self._last_session_mirror.pop(upload_id, None)
            logger.warning(
                "Expired-upload sweep: reclaimed upload %s (session %s) — parts deleted, session failed.",
                upload_id,
                session_id,
            )
            return 1

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

    async def _sync_pending_session_upload(self, upload: dict[str, Any], *, file_id: str | None = None) -> None:
        """Mirror transfer progress onto the session, at most every few seconds.

        Always mirrors when the part just stored completed its file, so the
        session never lags a finished transfer; between those points the mirror
        is rate-limited (see ``SESSION_UPLOAD_MIRROR_INTERVAL_SECONDS``).
        """
        upload_id = str(upload["id"])
        now = time.monotonic()
        file_record = self._file_by_kind_or_id(upload, file_id)
        file_done = bool(file_record) and int(file_record.get("uploadedBytes") or 0) >= int(file_record.get("sizeBytes") or 0) > 0
        last = self._last_session_mirror.get(upload_id)
        if not file_done and last is not None and now - last < SESSION_UPLOAD_MIRROR_INTERVAL_SECONDS:
            return
        self._last_session_mirror[upload_id] = now

        upload_ref = {
            "id": upload["id"],
            "status": upload.get("status"),
            "strategy": upload.get("strategy"),
            "expiresAt": upload.get("expiresAt"),
        }
        pending_files = {
            "video": self._pending_file_meta(upload.get("files") or [], "video"),
            "caseStudy": self._pending_file_meta(upload.get("files") or [], "caseStudy"),
        }

        def mirror(current: dict[str, Any]) -> Any:
            if str(current.get("status")) not in {SessionStatus.WAITING_FOR_UPLOAD, SessionStatus.UPLOADING}:
                # The session has moved on (assembling, committed, failed by a
                # sweep); a late part must not drag it back to a transfer state.
                return False
            current["upload"] = upload_ref
            current["files"] = pending_files
            return None

        await self.sessions.update(str(upload["sessionId"]), mirror)

    @staticmethod
    def _file_by_kind_or_id(upload: dict[str, Any], file_id: str | None) -> dict[str, Any]:
        for file_record in upload.get("files") or []:
            if file_id and str(file_record.get("fileId")) == str(file_id):
                return file_record
        return {}

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
        # ffprobe needs a path. `materialize` is a no-op existence check on the
        # local backend and a cached download on a cloud one, so this validates
        # the bytes that were actually stored either way.
        local_path = await self.storage.materialize(storage_ref)
        storage_ref["localPath"] = str(local_path)
        try:
            await self.media.get_video_duration_seconds(local_path)
        except RuntimeError as error:
            message = str(error) or "Video validation failed."
            if "was not found in PATH" in message:
                raise AppError(message, status_code=500) from error
            raise AppError(
                "Uploaded video could not be read. Confirm the file is a valid video with readable metadata.",
                status_code=400,
            ) from error

    async def _validate_committed_case_study(self, storage_ref: dict[str, Any]) -> None:
        path = await self.storage.materialize(storage_ref)
        storage_ref["localPath"] = str(path)
        if path.suffix.lower() != ".pdf":
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
        if upload.get("status") in {UploadStatus.COMMITTED, UploadStatus.ABORTED, UploadStatus.EXPIRED}:
            return False
        expires = parse_iso(upload.get("expiresAt"))
        return expires is not None and datetime.now(timezone.utc) > expires

    def _assert_not_expired(self, upload: dict[str, Any]) -> None:
        if self._is_expired(upload):
            upload["status"] = UploadStatus.EXPIRED
            raise AppError("Upload session has expired.", status_code=410)
