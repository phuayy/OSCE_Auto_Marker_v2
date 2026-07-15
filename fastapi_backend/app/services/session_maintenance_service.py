from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.logging_utils import log_context
from app.repositories.notification_repository import NotificationRepository
from app.repositories.video_repository import VideoRepository
from app.services.assessment_service import AssessmentService
from app.services.job_queue_service import JobQueueService
from app.services.session_service import SessionService


logger = logging.getLogger(__name__)

# Session statuses that mean a job is (or is about to be) actively touching the
# session — rerunning one now would race the running pipeline.
_IN_FLIGHT_STATUSES = {"assembling", "queued", "processing"}

# Output artifacts whose files are owned by the session and safe to remove on
# delete/rerun. Deliberately excludes the case study (a shared, ref-counted
# rubric asset) and the source video (handled separately: a clip child's
# "video" is the parent's exported clip, not the child's to delete).
_OUTPUT_ARTIFACT_KEYS = (
    "audio",
    "whisperxJson",
    "transcript",
    "subtitle",
    "subtitleTrack",
    "audioProfessionalism",
    "communicationScores",
    "scores",
)


class SessionMaintenanceService:
    """Tear down a session (and its children) across every store, and re-run a
    session's scoring in place under the same id.

    Deletes span two databases (ORM + raw-SQL jobs) and the filesystem, so they
    cannot be one atomic transaction. Jobs are cancelled first (so a live local
    worker stops writing the about-to-be-removed session), DB rows are removed
    before files, and each step is best-effort so a partial failure cannot wedge
    a session in a half-deleted state.
    """

    def __init__(
        self,
        settings: Settings,
        sessions: SessionService,
        assessments: AssessmentService,
        jobs: JobQueueService,
        notifications: NotificationRepository,
        videos: VideoRepository,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.assessments = assessments
        self.jobs = jobs
        self.notifications = notifications
        self.videos = videos

    async def delete_session(self, session_id: str) -> dict[str, Any]:
        """Delete a session and every child clip-assessment session, wiping all
        associated database rows (sessions, assessments + results + criteria,
        jobs, notifications, source videos) and on-disk artifacts."""
        target = await self.sessions.read(session_id)  # raises FileNotFoundError -> 404
        child_ids = await self.sessions.repository.list_child_ids(session_id)
        # Delete children first, parent last — after the children are gone the
        # parent has nothing dangling referring back to it.
        deleted: list[str] = []
        for child_id in child_ids:
            await self._delete_one(child_id, session=None)
            deleted.append(child_id)
        await self._delete_one(session_id, session=target)
        deleted.append(session_id)
        logger.info(
            "Deleted session %s and %d child session(s).",
            session_id,
            len(child_ids),
            extra=log_context(session_id, "session_delete"),
        )
        return {"deletedSessionIds": deleted}

    async def _delete_one(self, session_id: str, session: dict[str, Any] | None) -> None:
        # Load the payload (for file paths) before the row is gone, unless the
        # caller already handed it to us.
        if session is None:
            try:
                session = await self.sessions.read(session_id)
            except FileNotFoundError:
                session = None

        # Cancel + delete jobs first so the local worker stops writing the row.
        await self._safe("purge jobs", session_id, self.jobs.purge_session(session_id))
        await self._safe("delete assessments", session_id, self.assessments.delete_session_results(session_id))
        await self._safe("delete notifications", session_id, self.notifications.delete_for_session(session_id))
        await self._safe("delete source video row", session_id, self.videos.delete_for_session(session_id))
        await self._safe("delete session row", session_id, self.sessions.repository.delete(session_id))

        if session is not None:
            self._delete_artifacts(session)

    async def rerun_session(self, session_id: str) -> dict[str, Any]:
        """Re-run a session's full pipeline in place under the SAME id: reset its
        outputs/pipeline, wipe the old assessment rows and score artifacts, then
        enqueue a fresh ``process_session`` job. On completion the pipeline
        upserts results under the same session id, overwriting the old scores."""
        session = await self.sessions.read(session_id)
        status = str(session.get("status") or "").lower()
        if status in _IN_FLIGHT_STATUSES:
            raise AppError("This session is already being processed.", status_code=409)

        video_path = Path(str(((session.get("files") or {}).get("video") or {}).get("absolutePath") or ""))
        if not video_path.exists():
            raise AppError("Source video is missing — cannot re-run this session.", status_code=400)

        # Remove stale score artifacts + old assessment rows so a failed re-run
        # never leaves last run's scores behind masquerading as current.
        self._delete_artifacts(session, keep_clips=True, keep_owned_video=True)
        await self._safe("delete assessments", session_id, self.assessments.delete_session_results(session_id))

        session["outputs"] = {}
        session["error"] = None
        session["pipeline"] = {
            "startedAt": None,
            "endedAt": None,
            "runtimeSeconds": None,
            "mode": self.settings.whisperx_device,
        }
        session["status"] = "queued"
        await self.sessions.write(session)

        job = await self.jobs.enqueue(
            session_id,
            "process_session",
            {
                "parentSessionId": session.get("parentSessionId"),
                "clipId": (session.get("clipSource") or {}).get("clipId"),
                "rerun": True,
            },
        )
        session["job"] = self.jobs.public_job(job)
        await self.sessions.write(session)
        logger.info(
            "Re-run enqueued for session %s (job %s).",
            session_id,
            job.get("id"),
            extra=log_context(session_id, "session_rerun", job_id=str(job.get("id"))),
        )
        return {
            "session": self.sessions.public_session(session),
            "job": self.jobs.public_job(job),
        }

    def _delete_artifacts(
        self,
        session: dict[str, Any],
        *,
        keep_clips: bool = False,
        keep_owned_video: bool = False,
    ) -> None:
        """Remove on-disk artifacts recorded on the session. Best-effort: a
        missing or locked file must never fail the operation.

        Never touches the case study PDF (shared, ref-counted rubric asset)."""
        outputs = session.get("outputs") if isinstance(session.get("outputs"), dict) else {}
        for key in _OUTPUT_ARTIFACT_KEYS:
            item = outputs.get(key)
            if isinstance(item, dict):
                self._unlink(item.get("absolutePath"))

        if not keep_clips:
            clips = outputs.get("videoClips")
            if isinstance(clips, list):
                for clip in clips:
                    if isinstance(clip, dict):
                        self._unlink(clip.get("absolutePath"))
            # The whole per-session clip directory (parent owns its clips).
            self._rmtree(self.settings.paths.output_clips_dir / str(session.get("id") or ""))

        # A clip child's video IS the parent's exported clip — deleting it would
        # corrupt the parent. Only a top-level session owns its uploaded video.
        if not keep_owned_video and not session.get("parentSessionId"):
            video = (session.get("files") or {}).get("video") or {}
            self._unlink(video.get("absolutePath"))

    @staticmethod
    async def _safe(action: str, session_id: str, coro: Any) -> None:
        try:
            await coro
        except Exception:
            logger.warning(
                "Best-effort teardown step failed (%s) for session %s.",
                action,
                session_id,
                exc_info=True,
                extra=log_context(session_id, "session_delete"),
            )

    @staticmethod
    def _unlink(path_value: Any) -> None:
        if not path_value:
            return
        try:
            Path(str(path_value)).unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not delete artifact file %s.", path_value, exc_info=True)

    @staticmethod
    def _rmtree(path: Path) -> None:
        try:
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            logger.debug("Could not delete artifact directory %s.", path, exc_info=True)
