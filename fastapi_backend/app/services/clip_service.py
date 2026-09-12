from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.exceptions import AppError
from app.core.logging_utils import log_context
from app.domain.notifications import NotificationType
from app.domain.sessions import (
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
PERSON_DETECTION_STEP = "person_detection"
BELL_DETECTION_STEP = "bell_detection"
SEGMENTATION_STEPS = frozenset({PERSON_DETECTION_STEP, BELL_DETECTION_STEP})
SEGMENTATION_STEP_BY_METHOD = {
    "person": PERSON_DETECTION_STEP,
    "bells": BELL_DETECTION_STEP,
}

# Clip-export job states, as recorded on ``session.clipExport.status``.
EXPORT_QUEUED = "queued"
EXPORT_RUNNING = "running"
EXPORT_COMPLETED = "completed"
EXPORT_FAILED = "failed"
LIVE_EXPORT_STATUSES = frozenset({EXPORT_QUEUED, EXPORT_RUNNING})


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
    ) -> None:
        self.sessions = sessions
        self.events = events
        self.media = media
        self.pipeline = pipeline
        self.jobs = jobs
        self.notifications = notifications

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
        if requested in {"bells", "person"}:
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
            # Name the running step in the durable payload. The session-list
            # projection exposes it as `currentStep`, and the card's stage gauge
            # only trusts a `stepProgress` reading that belongs to the step it is
            # currently naming — otherwise a value left over from some other run
            # would render as a bar that never moves.
            pipeline = current.setdefault("pipeline", {})
            pipeline["currentStep"] = segmentation_step
            pipeline["stepProgress"] = None
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
                    if method == "person"
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
                current["status"] = SessionStatus.FAILED
                current["error"] = message
                self._clear_segmentation_progress(current)
                return None

            failed = await self._commit(session, fail)
            await self.events.publish(
                session_id,
                "status",
                {"code": "failed", "message": f"Auto-crop failed: {message}"},
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
        session_count = sum(1 for clip in clips if clip.get("kind") != "intermission")
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
        if method == "person":
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
                # left it, for the whole of the bell pass.
                def switch_step(current: dict[str, Any]) -> Any:
                    pipeline = current.setdefault("pipeline", {})
                    pipeline["currentStep"] = BELL_DETECTION_STEP
                    pipeline["stepProgress"] = None
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
            pipeline = current.setdefault("pipeline", {})
            if pipeline.get("currentStep") != step:
                return False
            pipeline["stepProgress"] = percent
            return None

        async with lock:
            await self._commit(session, mutate)

    @staticmethod
    def _clear_segmentation_progress(session: dict[str, Any]) -> None:
        """Drop the running-step markers once segmentation is over.

        Leaving them behind would let a terminal session carry a half-finished
        percentage into whatever reads it next.
        """
        pipeline = session.get("pipeline")
        if not isinstance(pipeline, dict):
            return
        if pipeline.get("currentStep") in SEGMENTATION_STEPS:
            pipeline["currentStep"] = None
        pipeline["stepProgress"] = None

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
            if kinds and 0 <= segment_index < len(kinds) and str(kinds[segment_index]).strip().lower() == "intermission":
                clip_range["kind"] = "intermission"
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
            if clip.get("kind") != "intermission":
                pending_total += 1

        requested_at = self.pipeline.now_iso()

        def plan(current: dict[str, Any]) -> Any:
            current.setdefault("outputs", {})["videoClips"] = clips
            current["clipExport"] = {
                "planId": plan_id,
                "status": EXPORT_QUEUED,
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

        job = await self.jobs.enqueue(session_id, "export_clips", {"clipCount": pending_total, "planId": plan_id})
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
        _ = payload
        session = await self.sessions.read(session_id)
        clips = session_clips(session)
        if not clips:
            raise AppError("This session has no clip export plan to run.", status_code=400)
        video_path = session_video_path(session)
        plan_id = str((session.get("clipExport") or {}).get("planId") or "")

        # Intermissions are timeline markers: no MP4 is cut and none is counted.
        pending = [clip for clip in clips if str(clip.get("kind") or "session") != "intermission"]
        total = len(pending)
        started_at = str((session.get("clipExport") or {}).get("startedAt") or self.pipeline.now_iso())

        def start(current: dict[str, Any]) -> Any:
            export = current.setdefault("clipExport", {})
            export.update(
                {
                    "status": EXPORT_RUNNING,
                    "total": total,
                    "completed": sum(1 for clip in session_clips(current) if not clip.get("isDraft") and clip.get("kind") != "intermission"),
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
                            break
                    current.setdefault("clipExport", {})["completed"] = _position
                    return None

                await self._commit(session, checkpoint)
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
        await self._prune_stale_plan_dirs(session_id, keep_plan_id=plan_id)

        if self.notifications is not None:
            plural = "s" if total != 1 else ""
            await self.notifications.emit(
                NotificationType.CLIPS_READY,
                "Clips ready",
                f"\"{session.get('name') or session_id}\" has been split into "
                f"{total} clip{plural} — ready for assessment.",
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
        return str(job.get("status") or "") in {"waiting_for_upload", "queued", "running"}

    async def recrop_clip(self, session_id: str, clip_id: str, start: float, end: float) -> dict[str, Any]:
        session = await self.sessions.read(session_id)
        clip = find_clip(session, clip_id)
        if end <= start:
            raise AppError("`end` must be greater than `start`.", status_code=400)
        video_path = session_video_path(session)
        video_duration = await self.media.get_video_duration_seconds(video_path)
        start_seconds = max(0.0, min(float(start), video_duration))
        end_seconds = max(0.0, min(float(end), video_duration))
        if end_seconds - start_seconds < self.media.settings.auto_crop_min_clip_seconds:
            raise AppError(f"Cropped duration must be at least {self.media.settings.auto_crop_min_clip_seconds}s.", status_code=400)
        # A recrop lives beside its plan's clips when the clip has one, so the
        # plan directory stays the unit of ownership for pruning.
        plan_id = str(clip.get("planId") or "")
        session_clip_dir = self.media.settings.paths.output_clips_dir / session_id
        if plan_id:
            session_clip_dir = session_clip_dir / plan_id
        session_clip_dir.mkdir(parents=True, exist_ok=True)
        safe_label = re.sub(r"[^a-zA-Z0-9-_]", "-", str(clip.get("label") or "clip")) or "clip"
        new_file_name = f"{session_id}-{safe_label}-recrop-{self._epoch_ms()}.mp4"
        output_path = session_clip_dir / new_file_name
        await self.media.crop_video_segment(
            input_path=video_path,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            output_path=output_path,
        )
        stats = output_path.stat()
        url_dir = f"/media/clips/{session_id}/{plan_id}" if plan_id else f"/media/clips/{session_id}"
        patch = {
            "start": start_seconds,
            "end": end_seconds,
            "fileName": new_file_name,
            "absolutePath": str(output_path),
            "url": f"{url_dir}/{new_file_name}",
            "sizeBytes": stats.st_size,
            "createdAt": self.pipeline.now_iso(),
            "isDraft": False,
        }

        def apply(current: dict[str, Any]) -> Any:
            find_clip(current, clip_id).update(patch)
            return None

        await self._commit(session, apply)
        return {
            "session": self.sessions.public_session(session),
            "clip": self._public_clip(find_clip(session, clip_id)),
        }

    async def assess_clip(self, session_id: str, clip_id: str) -> dict[str, Any]:
        """Create a child session for one exported clip and queue its assessment.

        Always queued, never run in the request. The child is a full pipeline
        run (transcription included), which is minutes to hours of work; the
        job queue owns retries, restart recovery and the session's status while
        it runs, and the clip row in the timeline shows the child's stage from
        the session index.
        """
        parent_session = await self.sessions.read(session_id)
        clip = find_clip(parent_session, clip_id)
        if str(clip.get("kind") or "").lower() == "intermission":
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

        new_session_id = str(uuid4())
        entries, used_keys = await self.sessions.ensure_names_for_index(await self.sessions.read_all_entries())
        _ = entries
        preferred_name = f"{parent_session.get('name') or parent_session.get('id') or 'Session'} - {clip.get('label') or 'Clip'}"
        clip_session = {
            "id": new_session_id,
            "name": self.sessions.reserve_unique_session_name(used_keys, preferred_name),
            "createdAt": self.pipeline.now_iso(),
            "status": SessionStatus.UPLOADED,
            "pipeline": {
                "startedAt": None,
                "endedAt": None,
                "runtimeSeconds": None,
                "mode": self.media.settings.whisperx_device,
            },
            "parentSessionId": parent_session["id"],
            "clipSource": {"clipId": clip.get("id"), "label": clip.get("label"), "planId": clip.get("planId")},
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
        await self.sessions.write(clip_session)
        job = await self.jobs.enqueue(
            new_session_id,
            "process_session",
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
        entries = await self.sessions.read_all_entries()
        child_sessions = [
            entry.session
            for entry in entries
            if str(entry.session.get("parentSessionId") or "") == str(parent_id)
        ]
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
            "kind": str(clip.get("kind") or "session"),
            "personCount": clip.get("personCount"),
            "fileName": clip.get("fileName"),
            "url": clip.get("url"),
            "sizeBytes": clip.get("sizeBytes"),
            # True until the export job has cut this clip's MP4. The timeline
            # renders drafts, but nothing can be assessed until they land.
            "isDraft": bool(clip.get("isDraft")),
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

    @staticmethod
    def _epoch_ms() -> int:
        import time

        return int(time.time() * 1000)
