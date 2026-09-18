from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.logging_utils import log_context
from app.domain.enums import TaskType, Workflow
from app.domain.sessions import IN_FLIGHT_STATUSES, SessionStatus, session_clips, session_video_path
from app.repositories.notification_repository import NotificationRepository
from app.repositories.video_repository import VideoRepository
from app.services.assessment_service import AssessmentService
from app.services.job_queue_service import JobQueueService
from app.services.session_service import SessionService
from app.services.session_artifacts import SessionArtifacts

logger = logging.getLogger(__name__)

# Output artifacts whose files are owned by the session and safe to remove on
# delete/rerun. Deliberately excludes the case study (a shared, ref-counted
# rubric asset) and the source video (handled separately: a clip child's
# "video" is the parent's exported clip, not the child's to delete).
#
# Not everything a run leaves behind is named on the session: each scorer also
# writes a crash-checkpoint sidecar beside its output, and a panel writes a
# whole directory. Both are removed alongside these; see ``_delete_artifacts``.
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

    # ------------------------------------------------------------------
    # Starting work on an existing session
    #
    # Every path that runs the pipeline against a stored session goes through
    # the job queue: ``POST /process``, ``POST /auto-crop`` and re-run. None of
    # them does the work in the request. A handler that awaited transcription
    # held the connection for an hour, and when the process died with it the
    # session stayed ``processing`` with no job row for startup recovery to
    # find: un-openable, un-rerunnable, only deletable.
    # ------------------------------------------------------------------

    async def start_processing(self, session_id: str) -> dict[str, Any]:
        """Queue the standard pipeline for a session that already has its sources.

        Resumable by design: the pipeline reuses whatever artifacts are already
        on disk, so this is also how a failed run is picked up where it stopped.
        Use :meth:`rerun_session` to score from scratch instead.
        """
        session = await self._ready_to_start(session_id)
        return await self._queue_job(
            session_id,
            TaskType.PROCESS_SESSION,
            {
                "parentSessionId": session.get("parentSessionId"),
                "clipId": (session.get("clipSource") or {}).get("clipId"),
            },
            stage="session_process",
        )

    async def start_auto_crop(self, session_id: str) -> dict[str, Any]:
        """Queue segmentation (bells or person detection) for a long recording."""
        session = await self._ready_to_start(session_id)
        return await self._queue_job(
            session_id,
            TaskType.AUTO_CROP,
            {"workflow": session.get("workflow"), "segmentation": session.get("segmentation")},
            stage="session_auto_crop",
        )

    async def _ready_to_start(self, session_id: str) -> dict[str, Any]:
        """The session, once it is safe to queue work on it: not already in
        flight, and with its source video where the job will look for it."""
        session = await self.sessions.read(session_id)
        if str(session.get("status") or "").lower() in IN_FLIGHT_STATUSES:
            raise AppError("This session is already being processed.", status_code=409)
        session_video_path(session)
        return session

    @staticmethod
    def _task_type_for(session: dict[str, Any]) -> str:
        """The job that *is* this session's run.

        A long recording's run is segmentation, not the standard pipeline:
        transcribing and scoring a two-hour multi-station tape burns GPU time
        and paid LLM calls to produce one meaningless sheet. Everything else —
        including a clip child, whose video is a single clip and which carries
        no ``workflow`` of its own — is ``process_session``. The parent check is
        explicit so a future create path that copies the parent's payload can
        never turn a child into a segmentation job. ``session_clips`` matches
        the browser's own rule (``workflow === 'long' || videoClips.length``),
        which also covers rows written before ``workflow`` was persisted.
        """
        if session.get("parentSessionId"):
            return TaskType.PROCESS_SESSION
        if str(session.get("workflow") or "").lower() == Workflow.LONG or session_clips(session):
            return TaskType.AUTO_CROP
        return TaskType.PROCESS_SESSION

    async def _queue_job(
        self,
        session_id: str,
        task_type: str,
        payload: dict[str, Any],
        *,
        stage: str,
    ) -> dict[str, Any]:
        """Move the session to ``queued`` and enqueue ``task_type`` for it.

        The status flips before the job exists so the card gauges "queued" and
        the workspace refuses to open the session from the first moment; the
        job's public record is attached afterwards through ``update``, because
        the local runner may already have claimed the job and written the
        session by the time the enqueue call returns.
        """

        def to_queued(current: dict[str, Any]) -> Any:
            current["status"] = SessionStatus.QUEUED
            current["error"] = None
            return None

        await self.sessions.update(session_id, to_queued)
        job = await self.jobs.enqueue(session_id, task_type, payload)
        public_job = self.jobs.public_job(job)

        def attach(current: dict[str, Any]) -> Any:
            current["job"] = public_job
            return None

        session = await self.sessions.update(session_id, attach)
        logger.info(
            "%s queued for session %s (job %s).",
            task_type,
            session_id,
            job.get("id"),
            extra=log_context(session_id, stage, job_id=str(job.get("id")), task_type=task_type),
        )
        return {"session": self.sessions.public_session(session), "job": public_job}

    async def rerun_session(self, session_id: str) -> dict[str, Any]:
        """Re-run a session's own run in place under the SAME id.

        *Its own* run: what a re-run means depends on the session. A standard
        session (and a clip child, whose video is one clip) resets its
        outputs/pipeline, drops the old assessment rows and score artifacts and
        re-queues ``process_session``. A long recording re-queues ``auto_crop``
        — segmentation is its run — keeping its exported MP4s and the child
        sessions cut from them, which stay valid assessments of clips taken
        from the same source video.
        """
        session = await self._ready_to_start(session_id)
        task_type = self._task_type_for(session)

        # Remove stale score artifacts + old assessment rows so a failed re-run
        # never leaves last run's scores behind masquerading as current. That
        # includes ``scores/panel/<id>/``: a re-run means "mark this again",
        # and a panel used to adopt the marker sheets written against the
        # previous transcript because nothing in the session's lifecycle owned
        # that dir.
        self._delete_artifacts(session, keep_clips=True, keep_owned_video=True)
        # Wipe the whole WhisperX artifact dir too: its cache lookup has a
        # latest-file fallback, so any unrecorded leftover JSON from an old
        # attempt would silently skip re-transcription on this re-run.
        self._rmtree(self.settings.paths.output_whisperx_dir / session_id)
        await self._safe("delete assessments", session_id, self.assessments.delete_session_results(session_id))

        reset_pipeline = {
            "startedAt": None,
            "endedAt": None,
            "runtimeSeconds": None,
            "mode": self.settings.whisperx_device,
        }

        def reset(current: dict[str, Any]) -> Any:
            if task_type == TaskType.AUTO_CROP:
                # Segmentation re-run. Clear only what was just deleted or
                # superseded: the standard-pipeline outputs whose files
                # ``_delete_artifacts`` unlinked above (leftovers from a run
                # that should never have happened), and the export record for
                # a plan this run is about to replace. The clip list stays —
                # ``ClipService.auto_crop_session_by_id`` replaces it wholesale
                # when it succeeds, so clearing it here would only cost the
                # user the timeline they adjusted if this run fails too.
                outputs = current.get("outputs")
                if isinstance(outputs, dict):
                    for key in _OUTPUT_ARTIFACT_KEYS:
                        outputs.pop(key, None)
                current["clipExport"] = None
            else:
                current["outputs"] = {}
            current["error"] = None
            current["pipeline"] = dict(reset_pipeline)
            return None

        await self.sessions.update(session_id, reset)
        if task_type == TaskType.AUTO_CROP:
            payload: dict[str, Any] = {
                "workflow": session.get("workflow"),
                "segmentation": session.get("segmentation"),
                "rerun": True,
            }
        else:
            payload = {
                "parentSessionId": session.get("parentSessionId"),
                "clipId": (session.get("clipSource") or {}).get("clipId"),
                "rerun": True,
            }
        return await self._queue_job(session_id, task_type, payload, stage="session_rerun")

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
        session_id = str(session.get("id") or "")
        if not SessionArtifacts.valid_id(session_id):
            return
        for path in SessionArtifacts(self.settings).owned_paths(
            session, keep_clips=keep_clips, keep_upload=keep_owned_video,
        ):
            if path.is_symlink() or not path.is_dir():
                self._unlink(path)
            else:
                self._rmtree(path)
        for key in _OUTPUT_ARTIFACT_KEYS:
            item = outputs.get(key)
            if isinstance(item, dict):
                self._unlink(item.get("absolutePath"))
                # ...and the scorer's crash-checkpoint sidecar beside it. The
                # assessor (single mode) and the adjudicator (panel) both write
                # one next to scores/<id>.json under a dotted name the session
                # row never records, so nothing else would ever remove it.
                self._unlink(self._checkpoint_sidecar(item.get("absolutePath")))

        if not keep_clips:
            clips = outputs.get("videoClips")
            if isinstance(clips, list):
                for clip in clips:
                    if isinstance(clip, dict):
                        self._unlink(clip.get("absolutePath"))
            # The whole per-session clip directory (parent owns its clips).
            # Guarded on the id: an empty one resolves to the clips *root*.
            if session_id:
                self._rmtree(self.settings.paths.output_clips_dir / session_id)

        # A clip child's video IS the parent's exported clip — deleting it would
        # corrupt the parent. Only a top-level session owns its uploaded video.
        if not keep_owned_video and not session.get("parentSessionId"):
            video = (session.get("files") or {}).get("video") or {}
            self._unlink(video.get("absolutePath"))

        # The panel marking directory — every marker's sheet, adjudication.json
        # and their own checkpoints — is the session's own artefact, with no
        # ``keep_`` case: unlike clips (a child may hold one) or the video (a
        # child's is the parent's), nobody else can be pointing at it. Removing
        # it here gives delete *and* re-run the right behaviour at once.
        if session_id:
            self._rmtree(self.settings.paths.output_scores_panel_dir / session_id)

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
    def _checkpoint_sidecar(path_value: Any) -> str | None:
        """The crash-checkpoint file a scorer writes beside its output.

        Mirrors ``scripts/scorer_checkpoint.checkpoint_path_for_output``. The
        API cannot import ``scripts/`` — the dependency runs the other way,
        through ``llm_bootstrap`` — so the two spellings are pinned against
        each other by ``tests/test_session_maintenance.py`` instead.
        """
        if not path_value:
            return None
        path = Path(str(path_value))
        return str(path.with_name(f".{path.name}.checkpoint.json"))

    @staticmethod
    def _rmtree(path: Path) -> None:
        try:
            if any(parent.is_symlink() for parent in path.parents):
                return
            if path.is_symlink():
                path.unlink(missing_ok=True)
            elif path.exists():
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            logger.debug("Could not delete artifact directory %s.", path, exc_info=True)
