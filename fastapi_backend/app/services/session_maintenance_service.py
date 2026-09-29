from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.logging_utils import log_context
from app.core.utils import utc_now_iso
from app.domain.enums import TaskType, Workflow
from app.domain.sessions import IN_FLIGHT_STATUSES, SessionStatus, session_clips, session_video_path
from app.repositories.notification_repository import NotificationRepository
from app.repositories.video_repository import VideoRepository
from app.services.assessment_service import AssessmentService
from app.services.job_queue_service import JobQueueService
from app.services.session_service import SessionMutator, SessionService
from app.services.session_artifacts import SessionArtifacts

# `app.storage` is intentionally not imported here for typing: the storage
# service's own module is another agent's concurrent work, and this file only
# ever calls the one method on the `ObjectStorage` protocol its docstring
# promises (`delete_committed_object`). Duck-typed via `Any` so this file
# never fails to import while that lands.

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
    before files. Every DB step is REQUIRED, not best-effort: a step that fails
    stops that session's teardown before any file is touched and the failure
    propagates out of ``delete_session``, so a session is never reported
    deleted while a row of it (or a job still writing it) survives, and its
    files are never removed out from under a row that still exists. Because
    every repository delete used here is a no-op on an already-missing row,
    the whole operation is idempotent: a retry after a failure picks up
    exactly where it stopped rather than erroring on what the first attempt
    already finished. Artifact removal (files, then — for a top-level
    session's video — the committed object storage) stays best-effort and
    runs only once every DB step for that session has succeeded.
    """

    def __init__(
        self,
        settings: Settings,
        sessions: SessionService,
        assessments: AssessmentService,
        jobs: JobQueueService,
        notifications: NotificationRepository,
        videos: VideoRepository,
        *,
        storage: Any | None = None,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.assessments = assessments
        self.jobs = jobs
        self.notifications = notifications
        self.videos = videos
        # Keyword-only, default None: existing direct-construction call sites
        # (and this module's own tests that pass ``None`` for every other
        # collaborator too) keep working with object-storage deletion simply
        # skipped, the same way a caller with no storage backend configured
        # would see it.
        self.storage = storage

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

        # Every step here is required, in this order: jobs first (so a live
        # local worker stops writing the row before anything else moves), the
        # session row last (so a crash after some rows are gone still leaves
        # something for a retry to find and finish). The first failure raises
        # straight out of this method — see ``_required`` — before artifact
        # removal runs, so a DB failure never leaves a visible session whose
        # files are already gone.
        delete_failure_message = "Session deletion did not complete; nothing more was removed. Try again."
        await self._required(
            "purge jobs", session_id, self.jobs.purge_session(session_id),
            failure_message=delete_failure_message, stage="session_delete",
        )
        await self._required(
            "delete assessments", session_id, self.assessments.delete_session_results(session_id),
            failure_message=delete_failure_message, stage="session_delete",
        )
        await self._required(
            "delete notifications", session_id, self.notifications.delete_for_session(session_id),
            failure_message=delete_failure_message, stage="session_delete",
        )
        await self._required(
            "delete source video row", session_id, self.videos.delete_for_session(session_id),
            failure_message=delete_failure_message, stage="session_delete",
        )
        await self._required(
            "delete session row", session_id, self.sessions.repository.delete(session_id),
            failure_message=delete_failure_message, stage="session_delete",
        )

        if session is not None:
            self._delete_artifacts(session)
            await self._best_effort_delete_committed_video(session)

    # ------------------------------------------------------------------
    # Video retention
    #
    # A student's assessment record — transcript, scores, feedback — is text
    # this deployment has every reason to keep. The video it was scored from
    # is what fills a disk (storage/output/ growth) and carries a face and a
    # voice, so it is the one artifact worth deleting on a schedule rather
    # than only on an explicit delete/rerun. See session_video_retention_days
    # in core/config.py for the policy and how to disable it.
    # ------------------------------------------------------------------

    async def run_video_retention_sweep(self) -> int:
        """Purge every top-level session's stored video past the configured
        retention window, in one bounded batch.

        Bounded rather than exhaustive: a backlog larger than one batch (a
        fresh enable against years of old sessions) is worked down one sweep
        at a time rather than holding up the caller — startup, or the
        periodic loop below — until every candidate is done. Each purge is
        independent and best-effort, so one failure (a locked file, a session
        deleted mid-sweep) never stops the rest.
        """
        retention_days = self.settings.session_video_retention_days
        if retention_days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        candidates = await self.sessions.repository.list_video_retention_candidates(cutoff)
        purged = 0
        for session_id in candidates:
            try:
                if await self.purge_expired_video(session_id):
                    purged += 1
            except Exception:
                logger.warning(
                    "Video-retention purge failed for session %s; will retry on the next sweep.",
                    session_id,
                    exc_info=True,
                    extra=log_context(session_id, "session_video_retention"),
                )
        if purged:
            logger.info(
                "Video-retention sweep purged %d session video(s) older than %d day(s).",
                purged,
                retention_days,
            )
        return purged

    async def video_retention_sweep_loop(self) -> None:
        """Periodic companion to :meth:`run_video_retention_sweep`.

        Template: ``JobQueueService.stale_job_reaper_loop`` — sleep first,
        log and continue past a bad scan rather than let one end the loop, no
        ``CancelledError`` handler so shutdown (``BackgroundTaskRegistry
        .cancel_all()``) cancels it cleanly out of ``asyncio.sleep``.
        """
        interval = self.settings.session_retention_sweep_interval_seconds
        while True:
            await asyncio.sleep(interval)
            try:
                await self.run_video_retention_sweep()
            except Exception:
                logger.exception("Video-retention sweep failed; will retry on the next interval.")

    async def purge_expired_video(self, session_id: str) -> bool:
        """Delete one top-level session's stored video file, leaving its
        transcript, scores and every other artifact untouched.

        Idempotent and safe to call speculatively: a session with no video on
        disk, one already purged, or a clip-assessment child (whose
        ``files.video`` is its parent's exported clip, not its own) is a
        no-op that returns ``False``.
        """
        session = await self.sessions.read(session_id)
        if session.get("parentSessionId"):
            return False
        video = (session.get("files") or {}).get("video")
        absolute_path = video.get("absolutePath") if isinstance(video, dict) else None
        if not absolute_path:
            return False

        # Same removal the delete/re-run paths already use for this same
        # field (`_delete_artifacts` below), plus the committed object in
        # object storage under STORAGE_BACKEND=gcs (or any other backend
        # ``ObjectStorage.delete_committed_object`` is wired for). That call
        # is deliberately awaited BEFORE ``purgedAt`` is recorded, and its
        # exception is left to propagate: a failure here must stop the
        # purge, not report a video gone that the bucket still holds — the
        # sweep already retries a failed candidate on its next run.
        storage_ref = video.get("storageRef") if isinstance(video, dict) else None
        if self.storage is not None and isinstance(storage_ref, dict):
            await self.storage.delete_committed_object(storage_ref)
        self._unlink(absolute_path)
        purged_at = utc_now_iso()

        def mark_purged(current: dict[str, Any]) -> Any:
            files = current.get("files")
            current_video = files.get("video") if isinstance(files, dict) else None
            if not isinstance(current_video, dict) or not current_video.get("absolutePath"):
                return False
            current_video["absolutePath"] = None
            current_video["url"] = None
            current_video["purgedAt"] = purged_at
            return None

        await self.sessions.update(session_id, mark_purged)
        logger.info(
            "Purged video for session %s under the %d-day video-retention policy.",
            session_id,
            self.settings.session_video_retention_days,
            extra=log_context(session_id, "session_video_retention"),
        )
        return True

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

    async def _admit(self, session_id: str, *, reset: SessionMutator | None = None) -> dict[str, Any]:
        """Atomically claim the session for a run: one ``sessions.update``
        mutator that refuses (409) a session already in flight and otherwise
        flips it to ``queued``, applying ``reset`` (a rerun's outputs/pipeline
        reset) in the very same write.

        This is what makes admission exact under a race, not just the cheap
        pre-check in ``_ready_to_start``: ``SessionService.update`` re-reads
        and re-applies the mutator on a conflicting write, so of two
        concurrent admissions, whichever loses the underlying write sees the
        *other's* ``queued`` status on its re-read and raises 409 here instead
        of silently re-queuing (or, for a rerun, re-resetting) a session
        someone else already claimed. Folding the reset into the same mutator
        as the status flip is what keeps a rerun's destructive cleanup (in
        ``rerun_session``, after this call) safe to run unconditionally once
        it returns: nothing can be running the session while it has no job,
        because this write is what makes the job possible.
        """

        def admit(current: dict[str, Any]) -> Any:
            if str(current.get("status") or "").lower() in IN_FLIGHT_STATUSES:
                raise AppError("This session is already being processed.", status_code=409)
            current["status"] = SessionStatus.QUEUED
            current["error"] = None
            if reset is not None:
                reset(current)
            return None

        return await self.sessions.update(session_id, admit)

    async def _enqueue_and_attach(
        self,
        session_id: str,
        task_type: str,
        payload: dict[str, Any],
        *,
        stage: str,
    ) -> dict[str, Any]:
        """Enqueue ``task_type`` for a session ``_admit`` has already claimed,
        and attach the job's public record.

        A failed enqueue must not strand the session ``queued`` with no job to
        move it — that reads as "already in flight" to every later start or
        rerun request until a restart's ``reconcile_orphaned_sessions`` fails
        it for an unrelated reason. On failure this compensates immediately:
        flips the session to ``failed`` itself, guarded on it still being the
        ``queued`` state this call's own admission put it in, so a concurrent
        legitimate writer that has since moved the session on is never
        overwritten by a stale compensation.
        """
        try:
            job = await self.jobs.enqueue(session_id, task_type, payload)
        except Exception as error:
            logger.error(
                "Failed to enqueue %s for session %s; flipping it to failed rather than "
                "leaving it queued with no job able to move it.",
                task_type,
                session_id,
                exc_info=True,
                extra=log_context(session_id, stage, task_type=task_type),
            )

            await self._release_admission(session_id, "Could not queue this session's job; try again.")
            raise AppError(
                "Could not queue this session's job; try again.", status_code=503, retryable=True,
            ) from error

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

    async def _release_admission(self, session_id: str, message: str) -> None:
        """Undo a won admission that will get no job: flip ``queued`` to
        ``failed`` so the card offers Re-run instead of reading as in flight.

        Guarded on the session still being ``queued`` — the state this call's
        own admission put it in — so a concurrent legitimate writer that has
        since moved it on is never overwritten. Best-effort itself: if this
        write fails too, startup's ``reconcile_orphaned_sessions`` still fails
        a queued session with no job, and the caller's own error is the one
        worth surfacing.
        """

        def fail_if_still_queued(current: dict[str, Any]) -> Any:
            if current.get("status") != SessionStatus.QUEUED:
                return False
            current["status"] = SessionStatus.FAILED
            current["error"] = message
            return None

        try:
            await self.sessions.update(session_id, fail_if_still_queued)
        except Exception:
            logger.error(
                "Could not release the admission of session %s; startup reconciliation will fail it.",
                session_id,
                exc_info=True,
                extra=log_context(session_id, "session_admission"),
            )

    async def _queue_job(
        self,
        session_id: str,
        task_type: str,
        payload: dict[str, Any],
        *,
        stage: str,
    ) -> dict[str, Any]:
        """Admit the session (queued, atomically) and enqueue ``task_type`` for
        it. Used by :meth:`start_processing` / :meth:`start_auto_crop`, which
        have no reset to fold into the admission and no destructive cleanup to
        run in between — see :meth:`rerun_session` for the case that does.
        """
        await self._admit(session_id)
        return await self._enqueue_and_attach(session_id, task_type, payload, stage=stage)

    async def rerun_session(self, session_id: str) -> dict[str, Any]:
        """Re-run a session's own run in place under the SAME id.

        *Its own* run: what a re-run means depends on the session. A standard
        session (and a clip child, whose video is one clip) resets its
        outputs/pipeline, drops the old assessment rows and score artifacts and
        re-queues ``process_session``. A long recording re-queues ``auto_crop``
        — segmentation is its run — keeping its exported MP4s and the child
        sessions cut from them, which stay valid assessments of clips taken
        from the same source video.

        Admission happens before any of the destructive cleanup below: this
        used to delete artifacts and assessment rows first and only then try
        to admit, so a rerun that lost the 409 race could destroy a
        concurrently-running attempt's fresh outputs on its way to being
        refused (test_a_refused_rerun_never_deletes_the_running_sessions_artifacts).
        Admitting first means the cheap ``_ready_to_start`` check plus
        ``_admit``'s own race handling both run — and can both refuse — before
        a single file or row this call does not own is touched.
        """
        session = await self._ready_to_start(session_id)
        task_type = self._task_type_for(session)

        reset_pipeline = {
            "startedAt": None,
            "endedAt": None,
            "runtimeSeconds": None,
            "mode": self.settings.whisperx_device,
        }

        def reset(current: dict[str, Any]) -> None:
            if task_type == TaskType.AUTO_CROP:
                # Segmentation re-run. Clear only what was just deleted or
                # superseded: the standard-pipeline outputs whose files
                # ``_delete_artifacts`` unlinks below (leftovers from a run
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
            current["pipeline"] = dict(reset_pipeline)
            # status/error are set by ``_admit`` itself in the same write.

        await self._admit(session_id, reset=reset)

        # From here on this call has won the run: nothing else can be scoring
        # this session while it holds no job (``_admit`` is what makes a job
        # possible), so the destructive cleanup below is safe unconditionally.
        #
        # Remove stale score artifacts so a failed re-run never leaves last
        # run's scores behind masquerading as current. That includes
        # ``scores/panel/<id>/``: a re-run means "mark this again", and a
        # panel used to adopt the marker sheets written against the previous
        # transcript because nothing in the session's lifecycle owned that dir.
        self._delete_artifacts(session, keep_clips=True, keep_owned_video=True)
        # Wipe the whole WhisperX artifact dir too: its cache lookup has a
        # latest-file fallback, so any unrecorded leftover JSON from an old
        # attempt would silently skip re-transcription on this re-run.
        self._rmtree(self.settings.paths.output_whisperx_dir / session_id)
        # Required, not best-effort: a rerun whose assessment-row delete fails
        # must not proceed to re-queue a run that would leave the old rows
        # stale — and duplicated, once the fresh run finishes and writes its
        # own. Unlike the artifact cleanup above (files, safe to retry), a
        # left-behind assessment row is a second source of truth the next
        # successful run would silently coexist with.
        try:
            await self._required(
                "delete assessments", session_id, self.assessments.delete_session_results(session_id),
                failure_message="Re-run could not clear this session's previous results; try again.",
                stage="session_rerun",
            )
        except AppError as error:
            # Admission already flipped the session to queued; with no job
            # coming, leaving it there would strand it exactly as a failed
            # enqueue would.
            await self._release_admission(session_id, error.message)
            raise

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
        return await self._enqueue_and_attach(session_id, task_type, payload, stage="session_rerun")

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
    async def _required(
        action: str, session_id: str, coro: Any, *, failure_message: str, stage: str,
    ) -> None:
        """Run one DB step that the caller's operation cannot proceed without;
        a failure stops that operation rather than being swallowed. Shared by
        ``_delete_one`` (a delete) and ``rerun_session`` (dropping the old
        assessment rows before re-queuing) — each names its own
        ``failure_message`` because "did not complete" means something
        different in each caller's context, and its own ``stage`` for the log.
        """
        try:
            await coro
        except Exception as error:
            logger.error(
                "Required step failed (%s) for session %s.",
                action,
                session_id,
                exc_info=True,
                extra=log_context(session_id, stage),
            )
            raise AppError(failure_message, status_code=500, retryable=True) from error

    async def _best_effort_delete_committed_video(self, session: dict[str, Any]) -> None:
        """Remove a top-level session's committed video object from object
        storage, after every DB step for it has already succeeded.

        Best-effort and logged, not required: the session row is already
        gone by this point, so there is nothing left to mark on a failure —
        the object would simply be retried by a human operator or a future
        sweep, not by this call. (A durable retry queue for orphaned objects
        is out of scope here.) A clip child's video is its parent's exported
        clip, never its own to delete, so this only ever runs for a
        parent-less session. The case study PDF is a shared, ref-counted
        rubric asset and is never touched by any deletion path.
        """
        if self.storage is None or session.get("parentSessionId"):
            return
        video = (session.get("files") or {}).get("video")
        storage_ref = video.get("storageRef") if isinstance(video, dict) else None
        if not isinstance(storage_ref, dict):
            return
        session_id = str(session.get("id") or "")
        try:
            await self.storage.delete_committed_object(storage_ref)
        except Exception:
            logger.warning(
                "Could not delete committed video object for session %s; the session row is "
                "already gone, so this is left for an operator to clean up by hand.",
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
