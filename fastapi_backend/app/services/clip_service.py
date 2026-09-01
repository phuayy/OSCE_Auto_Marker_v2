from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.exceptions import AppError
from app.core.logging_utils import log_context
from app.domain.notifications import NotificationType
from app.pipeline.media import MediaPipeline
from app.services.event_service import EventService
from app.services.pipeline_service import PipelineService
from app.services.session_service import SessionService


logger = logging.getLogger(__name__)


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

    def _resolve_segmentation_method(self, session: dict[str, Any]) -> str:
        """Per-session segmentation choice (from the upload form), falling back
        to the server default. Unknown values degrade to the default rather
        than failing the job."""
        requested = str(session.get("segmentation") or "").strip().lower()
        if requested in {"bells", "person"}:
            return requested
        return self.media.settings.auto_crop_segmentation_default

    async def auto_crop_session_by_id(self, session_id: str, *, allow_processing: bool = False) -> dict[str, Any]:
        session = await self.sessions.read(session_id)
        if session.get("status") == "processing" and not allow_processing:
            raise AppError("This session is already being processed.", status_code=409)
        video_path = Path(str(((session.get("files") or {}).get("video") or {}).get("absolutePath") or ""))
        if not video_path.exists():
            raise RuntimeError("Video file is missing for this session.")
        method = self._resolve_segmentation_method(session)
        # Reflect the crop in the durable status so the session list can gauge
        # and gate it (in-flight sessions are not enterable). Without this the
        # legacy fire-and-forget /auto-crop path stays "uploaded" during the
        # crop and the session opens with an empty clip list.
        if session.get("status") != "processing":
            session["status"] = "processing"
            session["error"] = None
            await self.sessions.write(session)
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
            detection = await self._run_segmentation(session_id, method, video_path, video_duration)
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
            failed = await self.sessions.read(session_id)
            failed["status"] = "failed"
            failed["error"] = str(error) or type(error).__name__
            await self.sessions.write(failed)
            await self.events.publish(
                session_id,
                "status",
                {"code": "failed", "message": f"Auto-crop failed: {failed['error']}"},
            )
            # Auto-crop fails outside PipelineService.mark_session_failed, so it
            # must raise its own notification or a failed crop stays silent.
            if self.notifications is not None:
                await self.notifications.emit(
                    NotificationType.SESSION_FAILED,
                    "Auto-crop failed",
                    f'"{failed.get("name") or session_id}" could not be split into clips: {failed["error"]}',
                    session_id=session_id,
                )
            raise
        session.setdefault("outputs", {})["videoClips"] = clips
        session["status"] = "cropped"
        session["error"] = None
        await self.sessions.write(session)
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
        session_id: str,
        method: str,
        video_path: Path,
        video_duration: float,
    ) -> dict[str, Any]:
        """Run the chosen segmentation detector.

        Person detection failing (missing weights, OOM'd host, unreadable
        frames) degrades to bell detection instead of failing the whole
        auto-crop job; the fallback is surfaced in the SSE log AND recorded on
        ``source.fallbackFrom`` so the run stays auditable.
        """
        if method == "person":
            await self.events.publish(
                session_id,
                "log",
                {"source": "autocrop", "message": "Sampling frames and detecting people (RT-DETR)..."},
            )
            try:
                return await self.media.detect_person_clip_ranges_with_python(
                    video_path,
                    video_duration,
                    session_id=session_id,
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
                detection = await self._run_bell_segmentation(session_id, video_path, video_duration)
                detection["source"]["fallbackFrom"] = f"person_detection_failed: {reason[:500]}"
                return detection

        return await self._run_bell_segmentation(session_id, video_path, video_duration)

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
        if session.get("status") == "processing":
            raise AppError("This session is already being processed.", status_code=409)
        if self.jobs is None:
            raise RuntimeError("Durable job queue is not configured for clip export.")
        if await self._has_live_export(session):
            raise AppError(
                "A clip export is already running for this session. Wait for it to finish before re-splitting.",
                status_code=409,
            )

        video_path = Path(str(((session.get("files") or {}).get("video") or {}).get("absolutePath") or ""))
        if not video_path.exists():
            raise RuntimeError("Video file is missing for this session.")
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
        source = {
            "type": "manual_timeline_split",
            "boundariesCount": len(boundaries or []),
            "labelsProvided": len(labels or []) > 0,
            "bellEndOffsetSeconds": self.media.settings.bell_end_offset_seconds,
        }
        clips = self.media.build_clip_drafts_from_ranges(clip_ranges, video_duration, source, aligned_labels)
        # exportIndex fixes each clip's output file name for the life of the
        # plan. It has to survive retries unchanged, or a resumed export would
        # look for its finished clips under different names and cut them again.
        pending_total = 0
        for index, clip in enumerate(clips):
            clip["exportIndex"] = index
            if clip.get("kind") != "intermission":
                pending_total += 1

        session.setdefault("outputs", {})["videoClips"] = clips
        session["clipExport"] = {
            "status": "queued",
            "total": pending_total,
            "completed": 0,
            "requestedAt": self.pipeline.now_iso(),
            "startedAt": None,
            "endedAt": None,
            "error": None,
            "jobId": None,
        }
        await self.sessions.write(session)

        job = await self.jobs.enqueue(session_id, "export_clips", {"clipCount": pending_total})
        # Re-read: enqueue writes the job projection onto the session, so the
        # in-memory copy above is already stale.
        session = await self.sessions.read(session_id)
        export = session.setdefault("clipExport", {})
        export["jobId"] = job.get("id")
        await self.sessions.write(session)

        return {
            "session": self.sessions.public_session(session),
            "clipCount": pending_total,
            "intermissionCount": len(clips) - pending_total,
            "clipExport": dict(export),
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
        to "processing" would be closed underneath them.
        """
        _ = payload
        session = await self.sessions.read(session_id)
        clips = (session.get("outputs") or {}).get("videoClips")
        if not isinstance(clips, list) or not clips:
            raise AppError("This session has no clip export plan to run.", status_code=400)
        video_path = Path(str(((session.get("files") or {}).get("video") or {}).get("absolutePath") or ""))
        if not video_path.exists():
            raise RuntimeError("Video file is missing for this session.")

        # Intermissions are timeline markers: no MP4 is cut and none is counted.
        pending = [clip for clip in clips if str(clip.get("kind") or "session") != "intermission"]
        total = len(pending)
        export = session.setdefault("clipExport", {})
        export.update(
            {
                "status": "running",
                "total": total,
                "completed": sum(1 for clip in pending if not clip.get("isDraft")),
                "startedAt": export.get("startedAt") or self.pipeline.now_iso(),
                "endedAt": None,
                "error": None,
            }
        )
        await self.sessions.write(session)
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
                )
                # Persist after every clip: this is the checkpoint an interrupted
                # or retried run resumes from.
                session.setdefault("outputs", {})["videoClips"] = clips
                session["clipExport"]["completed"] = position
                await self.sessions.write(session)
                logger.info(
                    "Clip export %d/%d %s for session %s.",
                    position,
                    total,
                    "reused an existing file" if result.get("reused") else "cut a new file",
                    session_id,
                    extra=log_context(session_id, "clip_export_progress", clip_id=str(clip.get("id"))),
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
            failed = await self.sessions.read(session_id)
            failed_export = failed.setdefault("clipExport", {})
            failed_export.update(
                {
                    "status": "failed",
                    "endedAt": self.pipeline.now_iso(),
                    "error": str(error) or type(error).__name__,
                }
            )
            await self.sessions.write(failed)
            await self.events.publish(
                session_id,
                "status",
                {"code": "clip_export_failed", "message": f"Clip export failed: {failed_export['error']}"},
            )
            if self.notifications is not None:
                await self.notifications.emit(
                    NotificationType.SESSION_FAILED,
                    "Clip export failed",
                    f"\"{failed.get('name') or session_id}\" could not be split into clips: {failed_export['error']}",
                    session_id=session_id,
                )
            raise

        session = await self.sessions.read(session_id)
        session.setdefault("clipExport", {}).update(
            {
                "status": "completed",
                "completed": total,
                "endedAt": self.pipeline.now_iso(),
                "error": None,
            }
        )
        # A session mid-assessment keeps the status it earned; only a session
        # still sitting on its upload is promoted to "cropped".
        if session.get("status") not in {"completed", "processing"}:
            session["status"] = "cropped"
        await self.sessions.write(session)

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

    @staticmethod
    def _active_export(session: dict[str, Any]) -> dict[str, Any] | None:
        """The clip export this session believes is in flight, if any."""
        export = session.get("clipExport")
        if isinstance(export, dict) and str(export.get("status")) in {"queued", "running"}:
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
        clips = session.get("outputs", {}).get("videoClips") if isinstance(session.get("outputs"), dict) else []
        if not isinstance(clips, list):
            clips = []
        clip = next((item for item in clips if str(item.get("id")) == str(clip_id)), None)
        if clip is None:
            raise AppError("Clip not found for this session.", status_code=404)
        if end <= start:
            raise AppError("`end` must be greater than `start`.", status_code=400)
        video_path = Path(str(((session.get("files") or {}).get("video") or {}).get("absolutePath") or ""))
        video_duration = await self.media.get_video_duration_seconds(video_path)
        start_seconds = max(0.0, min(float(start), video_duration))
        end_seconds = max(0.0, min(float(end), video_duration))
        if end_seconds - start_seconds < self.media.settings.auto_crop_min_clip_seconds:
            raise AppError(f"Cropped duration must be at least {self.media.settings.auto_crop_min_clip_seconds}s.", status_code=400)
        session_clip_dir = self.media.settings.paths.output_clips_dir / session_id
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
        clip.update(
            {
                "start": start_seconds,
                "end": end_seconds,
                "fileName": new_file_name,
                "absolutePath": str(output_path),
                "url": f"/media/clips/{session_id}/{new_file_name}",
                "sizeBytes": stats.st_size,
                "createdAt": self.pipeline.now_iso(),
                "isDraft": False,
            }
        )
        session.setdefault("outputs", {})["videoClips"] = clips
        await self.sessions.write(session)
        return {
            "session": self.sessions.public_session(session),
            "clip": self._public_clip(clip),
        }

    async def assess_clip(self, session_id: str, clip_id: str, defer: bool) -> dict[str, Any]:
        parent_session = await self.sessions.read(session_id)
        clips = (parent_session.get("outputs") or {}).get("videoClips")
        if not isinstance(clips, list):
            clips = []
        clip = next((item for item in clips if str(item.get("id")) == str(clip_id)), None)
        if clip is None:
            raise AppError("Clip not found.", status_code=404)
        if str(clip.get("kind") or "").lower() == "intermission":
            raise AppError(
                "This segment is an intermission (no confirmed session detected) and cannot be assessed.",
                status_code=400,
            )
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
            "status": "uploaded",
            "pipeline": {
                "startedAt": None,
                "endedAt": None,
                "runtimeSeconds": None,
                "mode": self.media.settings.whisperx_device,
            },
            "parentSessionId": parent_session["id"],
            "clipSource": {"clipId": clip.get("id"), "label": clip.get("label")},
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
            "outputs": self.empty_outputs(),
            "error": None,
        }
        await self.sessions.write(clip_session)
        if defer:
            if self.jobs is None:
                raise RuntimeError("Durable job queue is not configured for deferred clip assessment.")
            job = await self.jobs.enqueue(
                new_session_id,
                "process_session",
                {"parentSessionId": parent_session["id"], "clipId": clip.get("id")},
            )
            clip_session["status"] = "queued"
            clip_session["job"] = self.jobs.public_job(job)
            await self.sessions.write(clip_session)
            return {
                "session": self.sessions.public_session(clip_session),
                "parentSessionId": parent_session["id"],
                "clipId": clip.get("id"),
                "job": self.jobs.public_job(job),
            }
        try:
            payload = await self.pipeline.process_session_by_id(new_session_id)
        except Exception as error:
            await self.pipeline.mark_session_failed(new_session_id, error)
            raise
        return {**payload, "parentSessionId": parent_session["id"], "clipId": clip.get("id")}

    async def rename_clip(self, session_id: str, clip_id: str, label: str) -> dict[str, Any]:
        session = await self.sessions.read(session_id)
        clips = (session.get("outputs") or {}).get("videoClips")
        if not isinstance(clips, list):
            clips = []
        clip = next((item for item in clips if str(item.get("id")) == str(clip_id)), None)
        if clip is None:
            raise AppError("Clip not found.", status_code=404)
        clip["label"] = self.media.sanitize_clip_label(label, str(clip.get("label") or "Student"))
        session.setdefault("outputs", {})["videoClips"] = clips
        await self.sessions.write(session)
        return {"session": self.sessions.public_session(session), "clip": self._public_clip(clip)}

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
        clips = (parent_session.get("outputs") or {}).get("videoClips")
        if not isinstance(clips, list):
            clips = []
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
        return {
            "audio": None,
            "whisperxJson": None,
            "transcript": None,
            "subtitle": None,
            "subtitleTrack": None,
            "audioProfessionalism": None,
            "communicationScores": None,
            "videoClips": None,
            "scores": None,
        }

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
