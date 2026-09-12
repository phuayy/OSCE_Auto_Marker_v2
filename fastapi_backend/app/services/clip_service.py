from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.exceptions import AppError
from app.core.locks import KeyedLocks
from app.core.logging_utils import log_context
from app.domain.enums import (
    ClipExportScope,
    ClipExportStatus,
    ClipKind,
    PipelineStep,
    SegmentationMethod,
    StepStatus,
    TaskType,
)
from app.domain.jobs import ACTIVE_JOB_STATUSES
from app.domain.notifications import NotificationType
from app.domain.session_lifecycle import fail_session, record_progress, sync_current_step
from app.domain.sessions import (
    IN_FLIGHT_STATUSES,
    SessionStatus,
    empty_outputs,
    find_clip,
    session_clips,
    session_video_path,
)
from app.pipeline import person_presets
from app.pipeline.media import MediaPipeline
from app.services.event_service import EventService
from app.services.pipeline_service import PipelineService
from app.services.session_service import SessionMutator, SessionService

logger = logging.getLogger(__name__)

# Step names auto-crop reports under. They are not part of the standard
# pipeline sequence — a long-workflow session only ever splits a recording —
# so the frontend gauges them separately (src/lib/processingStage.js keeps the
# matching list). Keep the two in step.
PERSON_DETECTION_STEP = PipelineStep.PERSON_DETECTION
BELL_DETECTION_STEP = PipelineStep.BELL_DETECTION
SEGMENTATION_STEPS = frozenset({PERSON_DETECTION_STEP, BELL_DETECTION_STEP})
SEGMENTATION_STEP_BY_METHOD = {
    SegmentationMethod.PERSON: PERSON_DETECTION_STEP,
    SegmentationMethod.BELLS: BELL_DETECTION_STEP,
}

# Clip-export job states, as recorded on ``session.clipExport.status``.
EXPORT_QUEUED = ClipExportStatus.QUEUED
EXPORT_RUNNING = ClipExportStatus.RUNNING
EXPORT_COMPLETED = ClipExportStatus.COMPLETED
EXPORT_FAILED = ClipExportStatus.FAILED
LIVE_EXPORT_STATUSES = frozenset({EXPORT_QUEUED, EXPORT_RUNNING})

# What one export job was asked to cut, recorded on ``session.clipExport``.
# A recrop is an export of a single clip: same job, same progress record, same
# watchers in the editor — only the scope, and what the editor says when it
# finishes, differ.
EXPORT_SCOPE_PLAN = ClipExportScope.PLAN
EXPORT_SCOPE_CLIP = ClipExportScope.CLIP


def _new_plan_id() -> str:
    """Identity of one clip-export plan.

    A plan's clips are cut into ``clips/<session>/<planId>/``. The id — not the
    clip's position — is what makes "an MP4 already at the expected path is a
    finished cut" true: two plans with different boundaries can never resolve
    to the same file, so a re-split never adopts the previous split's footage.
    """
    return uuid4().hex[:12]


class ClipService:
    def __init__(
        self,
        sessions: SessionService,
        events: EventService,
        media: MediaPipeline,
        pipeline: PipelineService,
        jobs: Any | None = None,
        notifications: Any | None = None,
        maintenance: Any | None = None,
    ) -> None:
        self.sessions = sessions
        self.events = events
        self.media = media
        self.pipeline = pipeline
        self.jobs = jobs
        self.notifications = notifications
        # Re-running a clip's existing child session is the same operation the
        # session list's "Re-run" button performs, so it is borrowed from
        # SessionMaintenanceService rather than reimplemented here. Optional:
        # a container built without it simply cannot re-run a finished child.
        self.maintenance = maintenance
        # One clip, one child session. Two requests for the same clip — a
        # double click, a second tab, "Run selected" racing a single run —
        # must not both pass the "does a child exist?" check and each create
        # one. Per process, which is where the API accepts requests; a Hatchet
        # worker takes none.
        self._assess_locks = KeyedLocks()

    # ------------------------------------------------------------------
    # Document updates
    #
    # Every write goes through ``SessionService.update`` with a mutator that is
    # replayed on the current row. The auto-crop and export jobs run for
    # minutes while the user renames clips in the timeline editor, and the two
    # must not overwrite each other: the mutator patches the field it owns and
    # leaves the rest of the document as it finds it.
    # ------------------------------------------------------------------

    async def _commit(self, session: dict[str, Any], mutate: SessionMutator) -> dict[str, Any]:
        """Apply ``mutate`` to the stored row and refresh ``session`` in place."""
        fresh = await self.sessions.update(str(session["id"]), mutate)
        session.clear()
        session.update(fresh)
        return session

    def _resolve_segmentation_method(self, session: dict[str, Any]) -> str:
        """Per-session segmentation choice (from the upload form), falling back
        to the server default. Unknown values degrade to the default rather
        than failing the job."""
        requested = str(session.get("segmentation") or "").strip().lower()
        if requested in {SegmentationMethod.BELLS, SegmentationMethod.PERSON}:
            return requested
        return self.media.settings.auto_crop_segmentation_default

    @staticmethod
    def _resolve_person_options(session: dict[str, Any]) -> dict[str, Any]:
        """Occupancy rule for this session's person detection.

        Lenient by design: the upload API is what rejects a bad preset, and by
        the time a job runs the only useful answer to an unrecognised value is
        the default rule — a session that has been uploaded and queued must not
        die on a name this build no longer ships.
        """
        stored = session.get("segmentationOptions")
        return person_presets.resolve_options(stored if isinstance(stored, dict) else None)

    async def auto_crop_session_by_id(self, session_id: str, *, allow_processing: bool = False) -> dict[str, Any]:
        session = await self.sessions.read(session_id)
        if session.get("status") == SessionStatus.PROCESSING and not allow_processing:
            raise AppError("This session is already being processed.", status_code=409)
        video_path = session_video_path(session)
        method = self._resolve_segmentation_method(session)
        # Reflect the crop in the durable status so the session list can gauge
        # and gate it (in-flight sessions are not enterable). Without this the
        # legacy fire-and-forget /auto-crop path stays "uploaded" during the
        # crop and the session opens with an empty clip list.
        # Defaulted rather than indexed: a method this map has not caught up
        # with must cost the run its progress bar, not the run itself.
        segmentation_step = SEGMENTATION_STEP_BY_METHOD.get(method, BELL_DETECTION_STEP)

        def start(current: dict[str, Any]) -> Any:
            # Name the running step the same way the standard pipeline does:
            # record it in `steps` and let sync_current_step project
            # `currentStep`/`stepProgress` from that — the session-list
            # projection exposes the latter as `currentStep`/`stepProgress`,
            # and the card's stage gauge only trusts a `stepProgress` reading
            # that belongs to the step it is currently naming, otherwise a
            # value left over from some other run would render as a bar that
            # never moves.
            pipeline = current.setdefault("pipeline", {})
            steps = pipeline.setdefault("steps", {})
            steps[segmentation_step] = {"status": StepStatus.RUNNING}
            sync_current_step(pipeline)
            if current.get("status") != SessionStatus.PROCESSING:
                current["status"] = SessionStatus.PROCESSING
                current["error"] = None
            return None

        await self._commit(session, start)
        await self.events.publish(
            session_id,
            "milestone",
            {
                "code": "autocrop_started",
                "message": (
                    "detecting people on screen and building clip ranges (RT-DETR)"
                    if method == SegmentationMethod.PERSON
                    else "detecting bells and building clip ranges"
                ),
            },
        )
        try:
            video_duration = await self.media.get_video_duration_seconds(video_path)
            detection = await self._run_segmentation(session, method, video_path, video_duration)
            # The person detector partitions the WHOLE timeline (sessions +
            # greyed intermissions); the bell detector only emits sessions.
            clips = self.media.build_clip_drafts_from_ranges(
                detection.get("timelineSegments") or detection["clipRanges"],
                video_duration,
                detection["source"],
            )
        except Exception as error:
            # Leave a terminal status behind — a session stuck on "processing"
            # can never be opened or retried from the UI.
            message = str(error) or type(error).__name__

            def fail(current: dict[str, Any]) -> Any:
                fail_session(current, message)
                self._clear_segmentation_progress(current, status=StepStatus.FAILED)
                return None

            failed = await self._commit(session, fail)
            await self.events.publish(
                session_id,
                "status",
                {"code": ClipExportStatus.FAILED, "message": f"Auto-crop failed: {message}"},
            )
            # Auto-crop fails outside PipelineService.mark_session_failed, so it
            # must raise its own notification or a failed crop stays silent.
            if self.notifications is not None:
                await self.notifications.emit(
                    NotificationType.SESSION_FAILED,
                    "Auto-crop failed",
                    f'"{failed.get("name") or session_id}" could not be split into clips: {message}',
                    session_id=session_id,
                )
            raise

        def finish(current: dict[str, Any]) -> Any:
            current.setdefault("outputs", {})["videoClips"] = clips
            current["status"] = SessionStatus.CROPPED
            current["error"] = None
            self._clear_segmentation_progress(current)
            return None

        await self._commit(session, finish)
        # Counts are session clips only — intermissions are greyed timeline
        # markers, not student clips.
        session_count = sum(1 for clip in clips if clip.get("kind") != ClipKind.INTERMISSION)
        intermission_count = len(clips) - session_count
        if self.notifications is not None:
            await self.notifications.emit(
                NotificationType.CLIPS_READY,
                "Clips ready",
                f'"{session.get("name") or session_id}" has been split into '
                f"{session_count} clip{'s' if session_count != 1 else ''} — ready for assessment.",
                session_id=session_id,
            )
        await self.events.publish(
            session_id,
            "milestone",
            {
                "code": "autocrop_complete",
                "message": (
                    f"detected {session_count} session clip{'s' if session_count != 1 else ''}"
                    + (f" and {intermission_count} intermission{'s' if intermission_count != 1 else ''}"
                       if intermission_count else "")
                ),
            },
        )
        return {
            "session": self.sessions.public_session(session),
            "clipCount": session_count,
            "intermissionCount": intermission_count,
            "source": detection["source"],
        }

    async def _run_segmentation(
        self,
        session: dict[str, Any],
        method: str,
        video_path: Path,
        video_duration: float,
    ) -> dict[str, Any]:
        """Run the chosen segmentation detector.

        Person detection failing (missing weights, OOM'd host, unreadable
        frames) degrades to bell detection instead of failing the whole
        auto-crop job; the fallback is surfaced in the SSE log AND recorded on
        ``source.fallbackFrom`` so the run stays auditable.

        Takes the session document rather than just its id because the person
        detector reports progress, and that progress has to be persisted: see
        ``_record_segmentation_progress``.
        """
        session_id = str(session.get("id") or "")
        if method == SegmentationMethod.PERSON:
            options = self._resolve_person_options(session)
            await self.events.publish(
                session_id,
                "log",
                {
                    "source": "autocrop",
                    "message": (
                        "Sampling frames and detecting people (RT-DETR), occupancy rule "
                        f"'{options['preset']}': at least {options['minPeople']} person(s) on screen."
                    ),
                },
            )
            # Readings arrive from the subprocess reader threads; the lock makes
            # the session document a single-writer resource for their duration.
            progress_lock = asyncio.Lock()
            try:
                return await self.media.detect_person_clip_ranges_with_python(
                    video_path,
                    video_duration,
                    session_id=session_id,
                    options=options,
                    on_progress=lambda percent: self._record_segmentation_progress(
                        session, PERSON_DETECTION_STEP, percent, lock=progress_lock
                    ),
                )
            except Exception as error:  # noqa: BLE001 — degrade to bells, keep the job alive
                reason = str(error) or type(error).__name__
                logger.warning(
                    "Person segmentation failed for session %s; falling back to bell detection: %s",
                    session_id,
                    reason,
                    extra=log_context(session_id, "autocrop_person_fallback"),
                )
                await self.events.publish(
                    session_id,
                    "log",
                    {
                        "source": "autocrop",
                        "message": f"Human detection failed ({reason[:300]}). Falling back to bell detection...",
                    },
                )

                # The card must stop gauging a step that is no longer running:
                # the bar would otherwise freeze wherever the failed detector
                # left it, for the whole of the bell pass. Retiring the person
                # step to `failed` (rather than deleting it) and re-deriving
                # through sync_current_step is what makes this the same
                # mechanism the standard pipeline uses, not a hand-rolled one
                # that could disagree with it about what "current" means.
                def switch_step(current: dict[str, Any]) -> Any:
                    pipeline = current.setdefault("pipeline", {})
                    steps = pipeline.setdefault("steps", {})
                    person_state = steps.get(PERSON_DETECTION_STEP)
                    if isinstance(person_state, dict):
                        person_state["status"] = StepStatus.FAILED
                    steps[BELL_DETECTION_STEP] = {"status": StepStatus.RUNNING}
                    sync_current_step(pipeline)
                    return None

                async with progress_lock:
                    await self._commit(session, switch_step)
                detection = await self._run_bell_segmentation(session_id, video_path, video_duration)
                detection["source"]["fallbackFrom"] = f"person_detection_failed: {reason[:500]}"
                return detection

        return await self._run_bell_segmentation(session_id, video_path, video_duration)

    async def _record_segmentation_progress(
        self,
        session: dict[str, Any],
        step: str,
        percent: float,
        *,
        lock: asyncio.Lock,
    ) -> None:
        """Persist a live completion percentage for the running detector.

        Two jobs in one write, and the second is the less obvious one:

        * The session-list projection reads ``pipeline.stepProgress``, so this
          is what turns the long-workflow card's fixed midpoint into a bar that
          moves.
        * It is the *only* thing auto-crop writes between "started" and
          "finished". Without it a detection run leaves the sessions table
          untouched for minutes, the change stream has nothing to announce, and
          any dashboard whose refresh failed in that window has no signal left
          to recover on.

        A reading for a step that is no longer the current one is dropped
        rather than resurrecting a finished step.
        """

        def mutate(current: dict[str, Any]) -> Any:
            return record_progress(current, step, percent, tracked_step=False)

        async with lock:
            await self._commit(session, mutate)

    @staticmethod
    def _clear_segmentation_progress(session: dict[str, Any], *, status: str = StepStatus.COMPLETED) -> None:
        """Retire whichever segmentation step is still ``running`` once
        auto-crop is over, then re-derive `currentStep`/`stepProgress` from
        `steps` via ``sync_current_step``.

        Leaving a step marked ``running`` behind would let a terminal session
        carry a half-finished percentage into whatever reads it next, and
        would leave that step outranking anything a *later* auto-crop run
        starts. ``status`` lets the two callers say which way this ended:
        the failure path passes ``StepStatus.FAILED``; a successful crop
        keeps the default.
        """
        pipeline = session.get("pipeline")
        if not isinstance(pipeline, dict):
            return
        steps = pipeline.get("steps")
        if isinstance(steps, dict):
            for name in SEGMENTATION_STEPS:
                state = steps.get(name)
                if isinstance(state, dict) and state.get("status") == StepStatus.RUNNING:
                    state["status"] = status
        sync_current_step(pipeline)

    async def _run_bell_segmentation(
        self,
        session_id: str,
        video_path: Path,
        video_duration: float,
    ) -> dict[str, Any]:
        await self.events.publish(
            session_id,
            "log",
            {"source": "autocrop", "message": "Extracting mono audio for bell detection..."},
        )
        return await self.media.detect_bell_clip_ranges_with_python(
            video_path,
            video_duration,
            sample_rate=self.media.settings.bell_detector_sample_rate,
        )

    # ------------------------------------------------------------------
    # Manual clip export
    #
    # Split into a request half and an execution half. Cutting N students out
    # of a two-hour recording is N ffmpeg passes — minutes of work that used to
    # run inline in the POST, holding the connection open with no job row, no
    # progress, and nothing to resume from if the process died on clip seven.
    # The request now records a durable plan and hands it to the job queue,
    # which already owns retries, backoff and restart recovery.
    # ------------------------------------------------------------------

    async def request_clip_export(
        self,
        session_id: str,
        boundaries: list[float],
        labels: list[str],
        kinds: list[str] | None = None,
    ) -> dict[str, Any]:
        """Record a clip-export plan and queue the job that carries it out.

        Returns as soon as the plan is persisted. The draft clips are visible on
        the session immediately, so the timeline renders the new segmentation
        while the MP4s are still being cut.
        """
        session = await self.sessions.read(session_id)
        if session.get("status") == SessionStatus.PROCESSING:
            raise AppError("This session is already being processed.", status_code=409)
        if self.jobs is None:
            raise RuntimeError("Durable job queue is not configured for clip export.")
        if await self._has_live_export(session):
            raise AppError(
                "A clip export is already running for this session. Wait for it to finish before re-splitting.",
                status_code=409,
            )

        video_path = session_video_path(session)
        video_duration = await self.media.get_video_duration_seconds(video_path)
        clip_ranges = self.media.build_manual_clip_ranges(video_duration, boundaries)
        if not clip_ranges:
            raise AppError("Manual separators did not produce valid clip ranges.", status_code=400)

        # Kinds and labels are positional per SEGMENT (as the client sees them).
        # Ranges carry their pre-filter segmentIndex, so a sub-minimum sliver
        # dropped by build_manual_clip_ranges cannot shift the mapping of every
        # segment after it. Missing/short kind lists default to "session" so
        # pre-kinds clients keep working unchanged.
        aligned_labels: list[str] = []
        for clip_range in clip_ranges:
            segment_index = int(clip_range.get("segmentIndex", -1))
            if kinds and 0 <= segment_index < len(kinds) and str(kinds[segment_index]).strip().lower() == ClipKind.INTERMISSION:
                clip_range["kind"] = ClipKind.INTERMISSION
            aligned_labels.append(
                labels[segment_index] if labels and 0 <= segment_index < len(labels) else ""
            )
        plan_id = _new_plan_id()
        source = {
            "type": "manual_timeline_split",
            "planId": plan_id,
            "boundariesCount": len(boundaries or []),
            "labelsProvided": len(labels or []) > 0,
            "bellEndOffsetSeconds": self.media.settings.bell_end_offset_seconds,
        }
        clips = self.media.build_clip_drafts_from_ranges(clip_ranges, video_duration, source, aligned_labels)
        # exportIndex fixes each clip's output file name for the life of the
        # plan, and planId fixes which plan's directory it lands in. Together
        # they must survive retries unchanged — a resumed export looks for its
        # finished clips under exactly these names — and differ between plans,
        # so the next split can never adopt this one's footage.
        pending_total = 0
        for index, clip in enumerate(clips):
            clip["exportIndex"] = index
            clip["planId"] = plan_id
            if clip.get("kind") != ClipKind.INTERMISSION:
                pending_total += 1

        requested_at = self.pipeline.now_iso()

        def plan(current: dict[str, Any]) -> Any:
            current.setdefault("outputs", {})["videoClips"] = clips
            current["clipExport"] = {
                "planId": plan_id,
                "status": EXPORT_QUEUED,
                # A whole split: every session clip in the new plan is cut.
                "scope": EXPORT_SCOPE_PLAN,
                "clipIds": None,
                "total": pending_total,
                "completed": 0,
                "requestedAt": requested_at,
                "startedAt": None,
                "endedAt": None,
                "error": None,
                "jobId": None,
            }
            return None

        await self._commit(session, plan)

        job = await self.jobs.enqueue(
            session_id,
            TaskType.EXPORT_CLIPS,
            {"clipCount": pending_total, "planId": plan_id, "scope": str(EXPORT_SCOPE_PLAN)},
        )
        job_id = job.get("id")

        def attach_job(current: dict[str, Any]) -> Any:
            export = current.setdefault("clipExport", {})
            if export.get("planId") != plan_id:
                # A newer plan replaced this one between the two writes; its
                # job id is not ours to set.
                return False
            export["jobId"] = job_id
            return None

        await self._commit(session, attach_job)
        export = dict(session.get("clipExport") or {})

        return {
            "session": self.sessions.public_session(session),
            "clipCount": pending_total,
            "intermissionCount": len(clips) - pending_total,
            "clipExport": export,
            "job": self.jobs.public_job(job),
        }

    async def export_clips_by_id(self, session_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Cut every session clip in the stored plan. The ``export_clips`` job.

        Resumable in two senses. Each finished clip is written to the session
        before the next crop starts, so an interrupted run loses at most the one
        clip in flight; and a clip whose MP4 is already on disk is adopted
        rather than re-cut, so a retry costs only the work that never completed.

        This handler owns the session's status field — the queue deliberately
        does not touch it (see app.services.job_tasks). The user sits in this
        session's timeline editor while the export runs, and a session flipped
        to "processing" would be closed underneath them. For the same reason
        every write here is a patch: the user's renames must survive the job.
        """
        session = await self.sessions.read(session_id)
        clips = session_clips(session)
        if not clips:
            raise AppError("This session has no clip export plan to run.", status_code=400)
        video_path = session_video_path(session)
        export_record = session.get("clipExport") or {}
        plan_id = str(export_record.get("planId") or "")

        # Intermissions are timeline markers: no MP4 is cut and none is counted.
        pending = [clip for clip in clips if str(clip.get("kind") or ClipKind.SESSION) != ClipKind.INTERMISSION]
        # A recrop queues the same job scoped to the clips whose boundaries
        # changed. The payload carries the ids rather than the service
        # re-deriving them, so a retry cuts exactly what the request asked for
        # even if the user has since dragged another separator.
        requested_ids = self._requested_clip_ids(payload, export_record)
        if requested_ids is not None:
            pending = [clip for clip in pending if str(clip.get("id")) in requested_ids]
            if not pending:
                raise AppError(
                    "The clips this export was queued for are no longer on this session.",
                    status_code=400,
                    retryable=False,
                )
        total = len(pending)
        pending_ids = {str(clip.get("id")) for clip in pending}
        started_at = str(export_record.get("startedAt") or self.pipeline.now_iso())

        def start(current: dict[str, Any]) -> Any:
            export = current.setdefault("clipExport", {})
            export.update(
                {
                    "status": EXPORT_RUNNING,
                    "total": total,
                    # Only the clips *this* job was asked for count towards its
                    # progress: a recrop of one clip in a session of twelve
                    # would otherwise open at 11/1, because every other clip is
                    # already cut.
                    "completed": sum(
                        1
                        for clip in session_clips(current)
                        if str(clip.get("id")) in pending_ids
                        and not clip.get("isDraft")
                        and clip.get("kind") != ClipKind.INTERMISSION
                    ),
                    "startedAt": started_at,
                    "endedAt": None,
                    "error": None,
                }
            )
            return None

        await self._commit(session, start)
        await self.events.publish(
            session_id,
            "milestone",
            {"code": "clip_export_started", "message": f"cutting {total} clip(s) from the recording"},
        )

        try:
            for position, clip in enumerate(pending, start=1):
                result = await self.media.materialize_clip(
                    session_id=session_id,
                    clip=clip,
                    video_path=video_path,
                    plan_id=plan_id or None,
                )
                materialized = dict(result["clip"])
                clip_id = str(materialized.get("id"))
                superseded = str(clip.get("supersededFile") or "")

                # Persist after every clip: this is the checkpoint an interrupted
                # or retried run resumes from. Only the cut clip's file fields
                # and the counter are touched — a label the user changed while
                # ffmpeg ran stays theirs.
                def checkpoint(current: dict[str, Any], _clip_id: str = clip_id, _position: int = position) -> Any:
                    for stored in session_clips(current):
                        if str(stored.get("id")) == _clip_id:
                            for key in ("fileName", "url", "absolutePath", "sizeBytes", "isDraft", "planId"):
                                if key in materialized:
                                    stored[key] = materialized[key]
                            # The replaced cut is only forgotten once the new one
                            # is durable, so an interrupted recrop still knows
                            # which file it is superseding on the next attempt.
                            stored.pop("supersededFile", None)
                            break
                    current.setdefault("clipExport", {})["completed"] = _position
                    return None

                await self._commit(session, checkpoint)
                await self._discard_superseded_cut(
                    session_id, superseded, replacement=str(materialized.get("absolutePath") or "")
                )
                logger.info(
                    "Clip export %d/%d %s for session %s.",
                    position,
                    total,
                    "reused an existing file" if result.get("reused") else "cut a new file",
                    session_id,
                    extra=log_context(session_id, "clip_export_progress", clip_id=clip_id),
                )
                await self.events.publish(
                    session_id,
                    "progress",
                    {
                        "code": "clip_export_progress",
                        "message": f"exported {position}/{total} clips",
                        "completed": position,
                        "total": total,
                    },
                )
        except Exception as error:
            # Terminal for the export only. The session keeps its status and its
            # clip list: the drafts are still a valid segmentation, and whatever
            # was already cut stays usable.
            message = str(error) or type(error).__name__
            ended_at = self.pipeline.now_iso()

            def fail(current: dict[str, Any]) -> Any:
                current.setdefault("clipExport", {}).update(
                    {"status": EXPORT_FAILED, "endedAt": ended_at, "error": message}
                )
                return None

            failed = await self._commit(session, fail)
            await self.events.publish(
                session_id,
                "status",
                {"code": "clip_export_failed", "message": f"Clip export failed: {message}"},
            )
            if self.notifications is not None:
                await self.notifications.emit(
                    NotificationType.SESSION_FAILED,
                    "Clip export failed",
                    f"\"{failed.get('name') or session_id}\" could not be split into clips: {message}",
                    session_id=session_id,
                )
            raise

        ended_at = self.pipeline.now_iso()

        def finish(current: dict[str, Any]) -> Any:
            current.setdefault("clipExport", {}).update(
                {"status": EXPORT_COMPLETED, "completed": total, "endedAt": ended_at, "error": None}
            )
            # A session mid-assessment keeps the status it earned; only a session
            # still sitting on its upload is promoted to "cropped".
            if current.get("status") not in {SessionStatus.COMPLETED, SessionStatus.PROCESSING}:
                current["status"] = SessionStatus.CROPPED
            return None

        await self._commit(session, finish)
        if requested_ids is None:
            # Only a whole split supersedes a plan. A recrop touches clips
            # inside the current plan's directory, so there is nothing to prune
            # and every other clip in that directory is still the live cut.
            await self._prune_stale_plan_dirs(session_id, keep_plan_id=plan_id)

        if self.notifications is not None:
            session_name = str(session.get("name") or session_id)
            plural = "s" if total != 1 else ""
            if requested_ids is None:
                title = "Clips ready"
                body = f'"{session_name}" has been split into {total} clip{plural} — ready for assessment.'
            else:
                labels = ", ".join(str(clip.get("label") or "clip") for clip in pending)
                title = "Clip re-cut"
                body = f'"{session_name}": {labels} re-cut — ready for assessment.'
            await self.notifications.emit(
                NotificationType.CLIPS_READY,
                title,
                body,
                session_id=session_id,
            )
        await self.events.publish(
            session_id,
            "milestone",
            {"code": "clip_export_complete", "message": f"exported {total} clip(s)"},
        )
        return {
            "session": self.sessions.public_session(session),
            "clipCount": total,
            "intermissionCount": len(clips) - total,
        }

    async def _prune_stale_plan_dirs(self, session_id: str, *, keep_plan_id: str) -> None:
        """Delete clip directories of superseded plans nothing refers to any more.

        A re-split leaves the previous plan's MP4s on disk. They cannot simply
        go: a child session assessed from one of them still plays and re-runs
        against that file. So a plan directory is removed only when it is not
        the current plan and no child session's video lives inside it.
        Best-effort — a locked file must never fail an export that succeeded.
        """
        if not keep_plan_id:
            return
        root = self.media.settings.paths.output_clips_dir / str(session_id)
        if not await asyncio.to_thread(root.is_dir):
            return
        referenced: set[str] = set()
        try:
            child_ids = await self.sessions.list_child_ids(session_id)
            for child_id in child_ids:
                try:
                    child = await self.sessions.read(child_id)
                except FileNotFoundError:
                    continue
                video = ((child.get("files") or {}).get("video") or {}).get("absolutePath")
                if video:
                    referenced.add(str(Path(str(video)).resolve().parent))
        except Exception:
            logger.debug("Could not enumerate clip children for %s; skipping plan pruning.", session_id, exc_info=True)
            return

        def _prune() -> list[str]:
            removed: list[str] = []
            for entry in root.iterdir():
                if not entry.is_dir() or entry.name == keep_plan_id:
                    continue
                if str(entry.resolve()) in referenced:
                    continue
                shutil.rmtree(entry, ignore_errors=True)
                removed.append(entry.name)
            return removed

        removed = await asyncio.to_thread(_prune)
        if removed:
            logger.info(
                "Pruned %d superseded clip plan directory(ies) for session %s: %s",
                len(removed),
                session_id,
                ", ".join(removed),
                extra=log_context(session_id, "clip_plan_prune"),
            )

    @staticmethod
    def _active_export(session: dict[str, Any]) -> dict[str, Any] | None:
        """The clip export this session believes is in flight, if any."""
        export = session.get("clipExport")
        if isinstance(export, dict) and str(export.get("status")) in LIVE_EXPORT_STATUSES:
            return export
        return None

    async def _has_live_export(self, session: dict[str, Any]) -> bool:
        """Whether an export is genuinely still running for this session.

        The session's own record is only a claim. A worker killed between
        starting a clip and finishing one leaves ``clipExport`` reading
        "running" forever, and trusting that alone would lock the user out of
        re-exporting with a 409 they can never clear. The job row is the
        authority: an export whose job has reached a terminal state (or vanished)
        is stale, and a fresh request is allowed to replace it.
        """
        export = self._active_export(session)
        if export is None:
            return False
        job_id = str(export.get("jobId") or "")
        if not job_id:
            return True
        try:
            job = await self.jobs.repository.read(job_id)
        except Exception:
            return False
        return str(job.get("status") or "") in ACTIVE_JOB_STATUSES

    async def request_clip_recrop(self, session_id: str, clip_id: str, start: float, end: float) -> dict[str, Any]:
        """Move one clip's boundaries and queue the job that re-cuts its MP4.

        A recrop is an export of a single clip, not a second way to run ffmpeg.
        Cutting it inside the request held the connection for as long as the
        crop took (a stream-copy that falls back to a re-encode is minutes on a
        long station), died with the process, reported no progress and left the
        superseded file on disk. Routing it through ``export_clips`` gives it
        the queue's retries and restart recovery, the same ``session.clipExport``
        progress record the editor already watches, and the same adoption rule
        that makes a retry cheap — for the price of one field, ``revision``,
        which is what stops the job adopting the cut it is replacing
        (see ``MediaPipeline.materialize_clip``).

        Returns as soon as the plan is persisted: the clip becomes a draft with
        its new range, which the timeline renders immediately.
        """
        session = await self.sessions.read(session_id)
        clip = find_clip(session, clip_id)
        if str(clip.get("kind") or ClipKind.SESSION) == ClipKind.INTERMISSION:
            raise AppError(
                "This segment is an intermission (no confirmed session detected) and is not cut to a file.",
                status_code=400,
            )
        if end <= start:
            raise AppError("`end` must be greater than `start`.", status_code=400)
        if session.get("status") == SessionStatus.PROCESSING:
            raise AppError("This session is already being processed.", status_code=409)
        if self.jobs is None:
            raise RuntimeError("Durable job queue is not configured for clip export.")
        if await self._has_live_export(session):
            raise AppError(
                "A clip export is already running for this session. Wait for it to finish before re-cutting.",
                status_code=409,
            )

        video_path = session_video_path(session)
        video_duration = await self.media.get_video_duration_seconds(video_path)
        start_seconds = max(0.0, min(float(start), video_duration))
        end_seconds = max(0.0, min(float(end), video_duration))
        if end_seconds - start_seconds < self.media.settings.auto_crop_min_clip_seconds:
            raise AppError(f"Cropped duration must be at least {self.media.settings.auto_crop_min_clip_seconds}s.", status_code=400)

        # The recrop stays inside the clip's own plan directory, so the plan
        # remains the unit of ownership for pruning and for child sessions that
        # play a clip from it.
        plan_id = str(clip.get("planId") or (session.get("clipExport") or {}).get("planId") or "")
        requested_at = self.pipeline.now_iso()

        def replan(current: dict[str, Any]) -> Any:
            target = find_clip(current, clip_id)
            target["start"] = start_seconds
            target["end"] = end_seconds
            target["revision"] = int(target.get("revision") or 0) + 1
            # A draft is a range with no file — exactly what this clip now is,
            # and what keeps it out of assessment until the new cut lands.
            previous = str(target.get("absolutePath") or "")
            if previous:
                target["supersededFile"] = previous
            target["isDraft"] = True
            for key in ("fileName", "url", "absolutePath", "sizeBytes"):
                target.pop(key, None)
            current["clipExport"] = {
                "planId": plan_id or None,
                "status": EXPORT_QUEUED,
                "scope": EXPORT_SCOPE_CLIP,
                "clipIds": [str(clip_id)],
                "total": 1,
                "completed": 0,
                "requestedAt": requested_at,
                "startedAt": None,
                "endedAt": None,
                "error": None,
                "jobId": None,
            }
            return None

        await self._commit(session, replan)

        job = await self.jobs.enqueue(
            session_id,
            TaskType.EXPORT_CLIPS,
            {
                "clipCount": 1,
                "planId": plan_id or None,
                "scope": str(EXPORT_SCOPE_CLIP),
                "clipIds": [str(clip_id)],
            },
        )
        job_id = job.get("id")

        def attach_job(current: dict[str, Any]) -> Any:
            export = current.setdefault("clipExport", {})
            if export.get("clipIds") != [str(clip_id)] or export.get("scope") != EXPORT_SCOPE_CLIP:
                # Another export replaced this one between the two writes; its
                # job id is not ours to set.
                return False
            export["jobId"] = job_id
            return None

        await self._commit(session, attach_job)
        return {
            "session": self.sessions.public_session(session),
            "clip": self._public_clip(find_clip(session, clip_id)),
            "clipExport": dict(session.get("clipExport") or {}),
            "job": self.jobs.public_job(job),
        }

    @staticmethod
    def _requested_clip_ids(
        payload: dict[str, Any] | None,
        export_record: dict[str, Any],
    ) -> set[str] | None:
        """Which clips this export job was queued for, or ``None`` for all of them.

        The job payload is the authority — it was written when the request was
        made and never changes — and the stored record is the fallback for a job
        row enqueued before the payload carried the ids.
        """
        for source in (payload or {}, export_record or {}):
            raw = source.get("clipIds")
            if isinstance(raw, (list, tuple)) and raw:
                return {str(item) for item in raw}
        return None

    async def _discard_superseded_cut(self, session_id: str, superseded: str, *, replacement: str) -> None:
        """Delete the MP4 a recrop replaced, unless a child still plays it.

        A child session assessed from the old cut keeps that file as its own
        source video: deleting it would break playback and any re-run of that
        assessment, exactly as ``_prune_stale_plan_dirs`` reasons about whole
        plan directories. Best-effort — a file that will not delete must never
        fail an export that succeeded.
        """
        if not superseded or not replacement or superseded == replacement:
            return
        superseded_path = Path(superseded)
        try:
            for child_id in await self.sessions.list_child_ids(session_id):
                try:
                    child = await self.sessions.read(child_id)
                except FileNotFoundError:
                    continue
                child_video = ((child.get("files") or {}).get("video") or {}).get("absolutePath")
                if child_video and Path(str(child_video)) == superseded_path:
                    logger.info(
                        "Kept the superseded cut %s: clip child %s still plays it.",
                        superseded_path.name,
                        child_id,
                        extra=log_context(session_id, "clip_recrop_keep_superseded"),
                    )
                    return
            await asyncio.to_thread(superseded_path.unlink, True)
        except Exception:
            logger.debug(
                "Could not delete the superseded clip file %s.", superseded, exc_info=True
            )

    async def assess_clip(self, session_id: str, clip_id: str) -> dict[str, Any]:
        """Assess one exported clip — idempotently, one child session per clip.

        Always queued, never run in the request. The child is a full pipeline
        run (transcription included), which is minutes to hours of work; the
        job queue owns retries, restart recovery and the session's status while
        it runs, and the clip row in the timeline shows the child's stage from
        the session index.

        *One clip has one child session*, and this method is what maintains
        that. A second request for a clip already being assessed returns the
        child that is running rather than minting another one — a double click,
        a second browser tab, or "Run selected" racing a single run used to
        create duplicate children, each scoring the same student, and the
        timeline row could then show whichever the session index happened to
        order first. A request for a clip whose child has *finished* re-runs
        that child in place, after refreshing what it points at: a clip re-cut
        since the last assessment has a new MP4, and re-running against the old
        one would score footage the user has already replaced.
        """
        async with self._assess_locks.hold(f"{session_id}:{clip_id}"):
            return await self._assess_clip_locked(session_id, clip_id)

    async def _assess_clip_locked(self, session_id: str, clip_id: str) -> dict[str, Any]:
        parent_session = await self.sessions.read(session_id)
        clip = find_clip(parent_session, clip_id)
        if str(clip.get("kind") or "").lower() == ClipKind.INTERMISSION:
            raise AppError(
                "This segment is an intermission (no confirmed session detected) and cannot be assessed.",
                status_code=400,
            )
        # Request validity before infrastructure: a caller asking for the wrong
        # clip gets the 400 that names it, not a 500 about the queue.
        if self.jobs is None:
            raise RuntimeError("Durable job queue is not configured for clip assessment.")
        clip_path = Path(str(clip.get("absolutePath") or ""))
        if not clip_path.exists():
            raise AppError("Clip file is missing. Export clips before assessment.", status_code=400)
        case_study = (parent_session.get("files") or {}).get("caseStudy") or {}
        case_study_path = Path(str(case_study.get("absolutePath") or ""))
        if not case_study_path.exists():
            raise AppError("Case study file is missing for this session.", status_code=400)

        existing = await self._existing_clip_child(session_id, clip_id)
        if existing is not None:
            return await self._resume_clip_child(parent_session, clip, existing)

        new_session_id = str(uuid4())
        preferred_name = f"{parent_session.get('name') or parent_session.get('id') or 'Session'} - {clip.get('label') or 'Clip'}"
        clip_session = {
            "id": new_session_id,
            "name": preferred_name,
            "createdAt": self.pipeline.now_iso(),
            "status": SessionStatus.UPLOADED,
            "pipeline": {
                "startedAt": None,
                "endedAt": None,
                "runtimeSeconds": None,
                "mode": self.media.settings.whisperx_device,
            },
            "parentSessionId": parent_session["id"],
            "clipSource": self._clip_source(clip),
            # Inherit the parent's transcription-corpus snapshot so the corpus
            # picked at upload biases every clip assessed within the session.
            "corpus": parent_session.get("corpus"),
            "files": {
                "video": {
                    "originalName": clip.get("fileName")
                    or f"{(((parent_session.get('files') or {}).get('video') or {}).get('originalName'))} ({clip.get('label') or 'clip'})",
                    "fileName": clip.get("fileName") or clip_path.name,
                    "absolutePath": str(clip_path),
                    "sizeBytes": int(clip.get("sizeBytes") or 0),
                    "mimeType": "video/mp4",
                    "url": clip.get("url") or f"/media/clips/{session_id}/{clip_path.name}",
                },
                "caseStudy": {
                    "originalName": case_study.get("originalName"),
                    "fileName": case_study.get("fileName"),
                    "absolutePath": case_study.get("absolutePath"),
                    "sizeBytes": case_study.get("sizeBytes"),
                    "mimeType": case_study.get("mimeType"),
                    "rubricAssetId": case_study.get("rubricAssetId"),
                    "rubricDeduplicated": case_study.get("rubricDeduplicated"),
                    "contentSha256": case_study.get("contentSha256"),
                },
            },
            "outputs": empty_outputs(),
            "error": None,
        }
        # The one legitimate whole-document write: the row does not exist yet.
        # Born ``queued`` so the child is gated from its first moment.
        clip_session["status"] = SessionStatus.QUEUED
        await self.sessions.create_named(clip_session)
        job = await self.jobs.enqueue(
            new_session_id,
            TaskType.PROCESS_SESSION,
            {"parentSessionId": parent_session["id"], "clipId": clip.get("id")},
        )
        # The local runner may already have claimed the job and written the row;
        # patch the job record on top of whatever is there.
        public_job = self.jobs.public_job(job)

        def attach_job(current: dict[str, Any]) -> Any:
            current["job"] = public_job
            return None

        clip_session = await self.sessions.update(new_session_id, attach_job)
        return {
            "session": self.sessions.public_session(clip_session),
            "parentSessionId": parent_session["id"],
            "clipId": clip.get("id"),
            "job": public_job,
            # False when this call created the child; the browser uses it to
            # say "queued" rather than "already queued".
            "reused": False,
        }

    @staticmethod
    def _clip_source(clip: dict[str, Any]) -> dict[str, Any]:
        """What a child session records about the clip it assesses.

        ``fileName`` and ``revision`` are the provenance a recrop makes
        necessary: they say *which cut* of the clip was assessed, so the
        timeline can tell a current assessment from one of footage that has
        since been replaced.
        """
        return {
            "clipId": clip.get("id"),
            "label": clip.get("label"),
            "planId": clip.get("planId"),
            "fileName": clip.get("fileName"),
            "revision": int(clip.get("revision") or 0),
        }

    async def _existing_clip_child(self, session_id: str, clip_id: str) -> dict[str, Any] | None:
        """The newest child session assessing this clip, if one exists."""
        # Looked up rather than called directly so a session store without the
        # query — a test double, or a build where it is absent — degrades to
        # the historical "always create a child" behaviour instead of failing
        # the request.
        finder = getattr(getattr(self.sessions, "repository", None), "find_clip_children", None)
        if not callable(finder):
            return None
        children = await finder(session_id, str(clip_id))
        return children[0] if children else None

    async def _resume_clip_child(
        self,
        parent_session: dict[str, Any],
        clip: dict[str, Any],
        child: dict[str, Any],
    ) -> dict[str, Any]:
        """Answer a repeat assessment request with the child that already exists.

        In flight: hand back that child and its job — the work the caller asked
        for is already happening. Terminal: point the child at the clip's
        *current* MP4 and re-run it in place, so a clip re-cut since the last
        assessment is scored as it is now rather than as it was.
        """
        child_id = str(child["id"])
        parent_id = str(parent_session["id"])
        clip_id = str(clip.get("id"))
        if str(child.get("status") or "") in IN_FLIGHT_STATUSES:
            session = await self.sessions.read(child_id)
            logger.info(
                "Clip %s is already being assessed by session %s; returning it.",
                clip_id,
                child_id,
                extra=log_context(parent_id, "clip_assess_reuse", clip_id=clip_id),
            )
            return {
                "session": self.sessions.public_session(session),
                "parentSessionId": parent_id,
                "clipId": clip.get("id"),
                "job": (session.get("job") or None),
                "reused": True,
            }

        if self.maintenance is None:
            raise RuntimeError("Session maintenance is not configured; a finished clip child cannot be re-run.")

        clip_path = Path(str(clip.get("absolutePath") or ""))
        clip_source = self._clip_source(clip)
        video_meta = {
            "originalName": clip.get("fileName") or clip_path.name,
            "fileName": clip.get("fileName") or clip_path.name,
            "absolutePath": str(clip_path),
            "sizeBytes": int(clip.get("sizeBytes") or 0),
            "mimeType": "video/mp4",
            "url": clip.get("url") or f"/media/clips/{parent_id}/{clip_path.name}",
        }

        def adopt_current_cut(current: dict[str, Any]) -> Any:
            current.setdefault("files", {})["video"] = video_meta
            current["clipSource"] = clip_source
            return None

        # Before the re-run, not after: the job may be claimed the moment it is
        # enqueued, and it must read the clip this request is about.
        await self.sessions.update(child_id, adopt_current_cut)
        logger.info(
            "Re-running the existing assessment of clip %s (session %s).",
            clip_id,
            child_id,
            extra=log_context(parent_id, "clip_assess_rerun", clip_id=clip_id),
        )
        result = await self.maintenance.rerun_session(child_id)
        return {
            **result,
            "parentSessionId": parent_id,
            "clipId": clip.get("id"),
            "reused": True,
        }

    async def rename_clip(self, session_id: str, clip_id: str, label: str) -> dict[str, Any]:
        session = await self.sessions.read(session_id)
        current_clip = find_clip(session, clip_id)
        next_label = self.media.sanitize_clip_label(label, str(current_clip.get("label") or "Student"))

        def rename(current: dict[str, Any]) -> Any:
            clip = find_clip(current, clip_id)
            if clip.get("label") == next_label:
                return False
            clip["label"] = next_label
            return None

        await self._commit(session, rename)
        return {"session": self.sessions.public_session(session), "clip": self._public_clip(find_clip(session, clip_id))}

    async def clip_summaries(self, parent_id: str) -> dict[str, Any]:
        try:
            parent_session = await self.sessions.read(parent_id)
        except FileNotFoundError as error:
            raise AppError("Parent session not found.", status_code=404) from error
        entries = await self.sessions.repository.child_summaries(parent_id)
        child_sessions = [entry.session for entry in entries]
        clips = session_clips(parent_session)
        clips_by_id = {str(clip.get("id")): clip for clip in clips if isinstance(clip, dict) and clip.get("id")}
        summaries: list[dict[str, Any]] = []
        for child in child_sessions:
            child_id = str(child.get("id") or "")
            clip_source = child.get("clipSource") if isinstance(child.get("clipSource"), dict) else {}
            current_clip_id = str(clip_source.get("clipId") or "")
            clip = clips_by_id.get(current_clip_id)
            clip_label = str(clip_source.get("label") or "").strip() or (clip or {}).get("label") or child.get("name") or f"Clip {child_id[:8]}"
            child_outputs = child.get("outputs") or {}
            content_scores = await self._read_json_if_exists((child_outputs.get("scores") or {}).get("absolutePath"))
            communication_scores = await self._read_json_if_exists(
                (child_outputs.get("communicationScores") or {}).get("absolutePath")
            )
            content_summary = content_scores.get("scoring_summary") if isinstance(content_scores, dict) else None
            communication_summary = communication_scores.get("scoring_summary") if isinstance(communication_scores, dict) else None
            content_criteria = content_scores.get("criteria") if isinstance(content_scores, dict) else []
            communication_criteria = communication_scores.get("criteria") if isinstance(communication_scores, dict) else []
            if not isinstance(content_criteria, list):
                content_criteria = []
            if not isinstance(communication_criteria, list):
                communication_criteria = []
            content_total = len(content_criteria) if content_criteria else int((content_summary or {}).get("total_criteria") or 0)
            content_yes = int((content_summary or {}).get("yes_count") or 0)
            content_percent = round((content_yes / content_total) * 1000) / 10 if content_total > 0 else 0
            summaries.append(
                {
                    "sessionId": child_id,
                    "sessionName": child.get("name") or None,
                    "clipId": current_clip_id,
                    "clipLabel": clip_label,
                    "clipOrder": self._clip_order(clips, current_clip_id),
                    "status": str(child.get("status") or "").lower(),
                    "content": {
                        "totalCriteria": content_total,
                        "yesCount": content_yes,
                        "noCount": int((content_summary or {}).get("no_count") or 0),
                        "criticalYes": int((content_summary or {}).get("critical_yes") or 0),
                        "criticalNo": int((content_summary or {}).get("critical_no") or 0),
                        "passFail": str((content_summary or {}).get("pass_fail") or ""),
                        "percentYes": content_percent,
                    }
                    if content_scores
                    else None,
                    "communication": {
                        "totalCriteria": int((communication_summary or {}).get("total_criteria") or len(communication_criteria)),
                        "totalScore": float((communication_summary or {}).get("total_score") or 0),
                        "maxScore": float((communication_summary or {}).get("max_score") or len(communication_criteria) * 3),
                        "passThreshold": float((communication_summary or {}).get("pass_threshold") or 0),
                        "passFail": str((communication_summary or {}).get("pass_fail") or ""),
                        "labelCounts": (communication_summary or {}).get("label_counts") or {},
                        "perCriterionPoints": [
                            {
                                "id": criterion.get("id", index + 1) if isinstance(criterion, dict) else index + 1,
                                "label": str((criterion or {}).get("label") or f"Criterion {index + 1}")
                                if isinstance(criterion, dict)
                                else f"Criterion {index + 1}",
                                "section": (criterion or {}).get("section") if isinstance(criterion, dict) else None,
                                "scoreLabel": str((criterion or {}).get("score_label") or "None")
                                if isinstance(criterion, dict)
                                else "None",
                                "points": float((criterion or {}).get("points") or 0) if isinstance(criterion, dict) else 0,
                            }
                            for index, criterion in enumerate(communication_criteria)
                        ],
                    }
                    if communication_scores
                    else None,
                }
            )
        summaries.sort(key=lambda item: (item["clipOrder"] if item["clipOrder"] >= 0 else 10**9, str(item["sessionId"])))
        assessed_count = len([item for item in summaries if item["content"] and item["communication"]])
        return {
            "parentSessionId": parent_id,
            "clipsCount": len(clips),
            "totalChildSessions": len(child_sessions),
            "assessedCount": assessed_count,
            "summaries": summaries,
        }

    @staticmethod
    def empty_outputs() -> dict[str, Any]:
        # Kept as a method for callers that still reach it here; the definition
        # lives with the session vocabulary in app.domain.sessions.
        return empty_outputs()

    @staticmethod
    def _public_clip(clip: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": clip.get("id"),
            "label": str(clip.get("label") or ""),
            "start": clip.get("start"),
            "end": clip.get("end"),
            "kind": str(clip.get("kind") or ClipKind.SESSION),
            "personCount": clip.get("personCount"),
            "fileName": clip.get("fileName"),
            "url": clip.get("url"),
            "sizeBytes": clip.get("sizeBytes"),
            # True until the export job has cut this clip's MP4. The timeline
            # renders drafts, but nothing can be assessed until they land.
            "isDraft": bool(clip.get("isDraft")),
            # How many times this clip has been re-cut. 0 for every clip a
            # split produces; each recrop bumps it, and the browser uses it to
            # tell a stale child assessment from a current one.
            "revision": int(clip.get("revision") or 0),
        }

    @staticmethod
    async def _read_json_if_exists(absolute_path: str | None) -> dict[str, Any] | None:
        if not absolute_path:
            return None
        path = Path(str(absolute_path))
        if not path.exists():
            return None
        try:
            raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            logger.debug(
                "Could not read clip summary JSON at %s; skipping.",
                path,
                exc_info=True,
                extra=log_context("", "clip_summary_read", path=str(path)),
            )
            return None

    @staticmethod
    def _clip_order(clips: list[dict[str, Any]], clip_id: str) -> int:
        for index, clip in enumerate(clips):
            if str(clip.get("id")) == str(clip_id):
                return index
        return -1
