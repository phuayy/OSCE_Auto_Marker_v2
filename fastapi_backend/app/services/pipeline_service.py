from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.exceptions import AppError
from app.core.json_utils import extract_json_object, write_json_file
from app.core.logging_utils import log_context
from app.core.utils import utc_now_iso
from app.pipeline.media import MediaPipeline
from app.pipeline.scoring import ScoringPipeline
from app.pipeline.transcript_correction import apply_replacements_to_file, correct_segments
from app.repositories.notification_repository import NotificationRepository
from app.services.assessment_service import AssessmentService
from app.services.event_service import EventService
from app.services.session_service import SessionService


logger = logging.getLogger(__name__)


class PipelineService:
    def __init__(
        self,
        sessions: SessionService,
        events: EventService,
        media: MediaPipeline,
        scoring: ScoringPipeline,
        assessments: AssessmentService | None = None,
        notifications: NotificationRepository | None = None,
    ) -> None:
        self.sessions = sessions
        self.events = events
        self.media = media
        self.scoring = scoring
        self.assessments = assessments
        self.notifications = notifications

    async def _notify_scoring_complete(self, session: dict[str, Any]) -> None:
        if self.notifications is None:
            return
        name = str(session.get("name") or session.get("id"))
        await self.notifications.notify(
            "Scoring complete",
            f'Scoring is complete for "{name}". Results are ready to review.',
            session_id=str(session["id"]),
        )

    async def mark_session_failed(self, session_id: str, error: Exception) -> None:
        message = self._exception_message(error, "Processing failed.")
        failed_step = "unknown"
        try:
            session = await self.sessions.read(session_id)
            failed_step = self._find_failed_step(session) or "unknown"
            session["status"] = "failed"
            pipeline = session.setdefault("pipeline", {})
            pipeline["endedAt"] = self.now_iso()
            if pipeline.get("startedAt"):
                runtime = self.runtime_seconds(str(pipeline["startedAt"]), pipeline["endedAt"])
                pipeline["runtimeSeconds"] = runtime
            session["error"] = message
            await self.sessions.write(session)
        except Exception:
            logger.exception("Failed to persist failed session state for session %s.", session_id)
        # Single, searchable line naming the step the pipeline failed at.
        logger.error(
            "Session processing failed at step '%s': %s",
            failed_step,
            message,
            extra=log_context(session_id, failed_step, status="failed"),
        )
        await self.events.publish(
            session_id,
            "status",
            {"code": "failed", "message": message, "failedStep": failed_step},
        )

    @staticmethod
    def _find_failed_step(session: dict[str, Any]) -> str | None:
        """Identify the step a run failed at: the most recently-updated failed
        step, falling back to the pipeline's current step."""
        pipeline = session.get("pipeline") or {}
        steps = pipeline.get("steps") if isinstance(pipeline.get("steps"), dict) else {}
        failed = [
            (str(name), str((state or {}).get("updatedAt") or ""))
            for name, state in steps.items()
            if isinstance(state, dict) and state.get("status") == "failed"
        ]
        if failed:
            failed.sort(key=lambda item: item[1], reverse=True)
            return failed[0][0]
        current = pipeline.get("currentStep")
        return str(current) if current else None

    async def process_session_by_id(self, session_id: str, *, allow_processing: bool = False) -> dict[str, Any]:
        session = await self.sessions.read(session_id)
        if session.get("status") == "processing" and not allow_processing:
            raise AppError("This session is already being processed.", status_code=409)

        outputs = session.get("outputs") or {}
        transcript_path_raw = (outputs.get("transcript") or {}).get("absolutePath")
        if transcript_path_raw:
            return await self._process_cached_transcript(session)
        return await self._process_from_video(session)

    async def _process_cached_transcript(self, session: dict[str, Any]) -> dict[str, Any]:
        session_id = str(session["id"])
        if await self.media.ensure_session_subtitle_track(session):
            await self.sessions.write(session)
        transcript_path = Path(str(session["outputs"]["transcript"]["absolutePath"]))
        transcript = await self._read_json(transcript_path)

        if self.media.settings.enable_audio_professionalism:
            await self._ensure_audio_output(session)

        scoring_outputs = await self._run_cached_scoring_branches(session)

        if session.get("status") != "completed":
            session["status"] = "completed"
            session["error"] = None
            pipeline = session.setdefault("pipeline", {})
            if not pipeline.get("startedAt"):
                pipeline["startedAt"] = self.now_iso()
            pipeline["endedAt"] = self.now_iso()
            pipeline["runtimeSeconds"] = self.runtime_seconds(str(pipeline["startedAt"]), str(pipeline["endedAt"]))
            await self.sessions.write(session)
            await self._notify_scoring_complete(session)
        await self._record_assessment_results(session)

        return {
            "session": self.sessions.public_session(session),
            "transcript": transcript,
            "audioProfessionalism": scoring_outputs.get("audioProfessionalism"),
            "communicationScores": scoring_outputs.get("communicationScores"),
            "scores": scoring_outputs.get("scores"),
        }

    async def _apply_corpus_corrections(
        self,
        session: dict[str, Any],
        transcript: dict[str, Any],
        whisperx_outputs: dict[str, Any],
    ) -> None:
        """Fuzzy-correct corpus terms in the normalized transcript and mirror
        the substitutions into the SRT/VTT files so player subtitles agree with
        what the scorers read. Best-effort: a correction failure must never
        fail the pipeline — the uncorrected transcript proceeds."""
        corpus = session.get("corpus") or {}
        terms = corpus.get("terms") or []
        transcript["corpusName"] = corpus.get("name")
        transcript["corrections"] = []
        if not terms:
            return
        try:
            corrections = correct_segments(
                transcript.get("segments") or [],
                terms,
                self.media.settings.transcript_correction_min_ratio,
            )
            transcript["corrections"] = corrections
            if not corrections:
                return
            for output_key in ("srtAbsolutePath", "vttAbsolutePath"):
                subtitle_path = whisperx_outputs.get(output_key)
                if subtitle_path:
                    await asyncio.to_thread(apply_replacements_to_file, Path(str(subtitle_path)), corrections)
            await self.events.publish(
                str(session["id"]),
                "log",
                {
                    "source": "transcript-correction",
                    "message": f"Applied {len(corrections)} corpus term correction(s) from '{corpus.get('name')}'.",
                },
            )
        except Exception:
            logger.exception("Corpus correction failed for session %s; using uncorrected transcript.", session.get("id"))

    async def _process_from_video(self, session: dict[str, Any]) -> dict[str, Any]:
        session_id = str(session["id"])
        pipeline = session.setdefault("pipeline", {})
        started_at = str(pipeline.get("startedAt") or self.now_iso())
        await self.events.publish(session_id, "milestone", {"code": "started", "message": "started"})
        session["status"] = "processing"
        session["error"] = None
        pipeline["startedAt"] = started_at
        pipeline["endedAt"] = None
        pipeline["runtimeSeconds"] = None
        await self.sessions.write(session)

        audio_info = await self._ensure_audio_output(session)

        await self._mark_pipeline_step(session, "whisperx", "running")
        try:
            whisperx_outputs = await self.media.run_whisperx_transcription(session, audio_info)
        except Exception as error:
            await self._mark_pipeline_step(session, "whisperx", "failed", error=error)
            raise
        await self._mark_pipeline_step(session, "whisperx", "completed")

        await self._mark_pipeline_step(session, "transcript_normalization", "running")
        whisperx_raw = await self._read_json(Path(str(whisperx_outputs["jsonAbsolutePath"])))
        normalized_transcript = self.media.normalize_whisperx_transcript(whisperx_raw)
        await self._apply_corpus_corrections(session, normalized_transcript, whisperx_outputs)

        transcript_file_name = f"{session_id}.json"
        transcript_path = self.media.settings.paths.output_transcripts_dir / transcript_file_name
        await asyncio.to_thread(write_json_file, transcript_path, normalized_transcript)

        whisperx_stats = Path(str(whisperx_outputs["jsonAbsolutePath"])).stat()
        transcript_stats = transcript_path.stat()
        session["outputs"]["whisperxJson"] = {
            "fileName": Path(str(whisperx_outputs["jsonAbsolutePath"])).name,
            "absolutePath": str(whisperx_outputs["jsonAbsolutePath"]),
            "sizeBytes": whisperx_stats.st_size,
        }
        srt_path = whisperx_outputs.get("srtAbsolutePath")
        if srt_path:
            srt_stats = Path(str(srt_path)).stat()
            session["outputs"]["subtitle"] = {
                "fileName": Path(str(srt_path)).name,
                "absolutePath": str(srt_path),
                "url": f"/media/whisperx/{session_id}/{Path(str(srt_path)).name}",
                "sizeBytes": srt_stats.st_size,
            }
        else:
            session["outputs"]["subtitle"] = None
        vtt_path = whisperx_outputs.get("vttAbsolutePath")
        if vtt_path:
            vtt_stats = Path(str(vtt_path)).stat()
            session["outputs"]["subtitleTrack"] = {
                "fileName": Path(str(vtt_path)).name,
                "absolutePath": str(vtt_path),
                "url": f"/media/whisperx/{session_id}/{Path(str(vtt_path)).name}",
                "sizeBytes": vtt_stats.st_size,
            }
        else:
            session["outputs"]["subtitleTrack"] = None
        session["outputs"]["transcript"] = {
            "fileName": transcript_file_name,
            "absolutePath": str(transcript_path),
            "url": f"/media/transcripts/{transcript_file_name}",
            "sizeBytes": transcript_stats.st_size,
        }
        await self._mark_pipeline_step(session, "transcript_normalization", "completed")
        await self.events.publish(
            session_id,
            "milestone",
            {"code": "transcription_complete", "message": "transcription/diarization complete"},
        )

        await self.sessions.write(session)
        scoring_outputs = await self._run_fresh_scoring_branches(session)
        audio_prof_payload = scoring_outputs.get("audioProfessionalism")
        communication_payload = scoring_outputs.get("communicationScores")
        scoring_payload = scoring_outputs.get("scores")

        ended_at = self.now_iso()
        session["status"] = "completed"
        session["pipeline"]["endedAt"] = ended_at
        session["pipeline"]["runtimeSeconds"] = self.runtime_seconds(started_at, ended_at)
        await self.sessions.write(session)
        await self._notify_scoring_complete(session)
        await self._record_assessment_results(session)
        await self.events.publish(
            session_id,
            "status",
            {"code": "completed", "message": "Processing completed successfully."},
        )
        return {
            "session": self.sessions.public_session(session),
            "transcript": normalized_transcript,
            "audioProfessionalism": audio_prof_payload,
            "communicationScores": communication_payload,
            "scores": scoring_payload,
        }

    async def _record_assessment_results(self, session: dict[str, Any]) -> None:
        if self.assessments is None:
            return
        await self._mark_pipeline_step(session, "assessment_persistence", "running")
        try:
            await self.assessments.record_session_results(session)
        except Exception as error:
            await self._mark_pipeline_step(session, "assessment_persistence", "failed", error=error)
            raise
        await self._mark_pipeline_step(session, "assessment_persistence", "completed")

    async def _ensure_audio_output(self, session: dict[str, Any]) -> dict[str, Any]:
        audio_path_raw = ((session.get("outputs") or {}).get("audio") or {}).get("absolutePath")
        session_id = str(session["id"])
        if audio_path_raw:
            audio_path = Path(str(audio_path_raw))
            if audio_path.exists() and audio_path.stat().st_size > 0:
                await self._mark_pipeline_step(
                    session,
                    "audio_extraction",
                    "completed",
                    metadata={"reusedExistingArtifact": True},
                )
                return dict(session["outputs"]["audio"])

        deterministic_audio_path = self.media.settings.paths.output_audio_dir / f"{session_id}.mp3"
        if deterministic_audio_path.exists() and deterministic_audio_path.stat().st_size > 0:
            audio_info = self._audio_output_metadata(session_id, deterministic_audio_path)
            session.setdefault("outputs", {})["audio"] = audio_info
            await self._mark_pipeline_step(
                session,
                "audio_extraction",
                "completed",
                metadata={"reusedExistingArtifact": True},
            )
            await self.sessions.write(session)
            await self.events.publish(
                session_id,
                "milestone",
                {"code": "converted_to_mp3", "message": f"reused existing mp3 ({audio_info['fileName']})"},
            )
            return audio_info

        await self._mark_pipeline_step(session, "audio_extraction", "running")
        try:
            audio_info = await self.media.extract_audio_to_mp3(session)
        except Exception as error:
            await self._mark_pipeline_step(session, "audio_extraction", "failed", error=error)
            raise
        session.setdefault("outputs", {})["audio"] = audio_info
        await self._mark_pipeline_step(
            session,
            "audio_extraction",
            "completed",
            metadata={"reusedExistingArtifact": False},
        )
        await self.sessions.write(session)
        await self.events.publish(
            session_id,
            "milestone",
            {"code": "converted_to_mp3", "message": f"converted to mp3 ({audio_info['fileName']})"},
        )
        return audio_info

    async def _mark_pipeline_step(
        self,
        session: dict[str, Any],
        step: str,
        status: str,
        *,
        metadata: dict[str, Any] | None = None,
        error: Exception | str | None = None,
        lock: asyncio.Lock | None = None,
    ) -> None:
        if lock is None:
            self._set_pipeline_step_state(session, step, status, metadata=metadata, error=error)
            await self.sessions.write(session)
        else:
            async with lock:
                self._set_pipeline_step_state(session, step, status, metadata=metadata, error=error)
                await self.sessions.write(session)
        self._log_pipeline_step(session, step, status, error=error)

    def _log_pipeline_step(
        self,
        session: dict[str, Any],
        step: str,
        status: str,
        *,
        error: Exception | str | None = None,
    ) -> None:
        """Emit a structured, searchable log line for each step transition.

        Logs step *start* (running) and *terminal* states (completed/failed/
        skipped) with a correlation id and per-stage latency, so a run — and
        crucially the exact step a failure occurred at — can be reconstructed
        from logs alone, not just the (in-process, non-durable) SSE stream.
        """
        session_id = str(session.get("id") or "")
        if status == "running":
            logger.info(
                "Pipeline step started: %s",
                step,
                extra=log_context(session_id, step, status=status),
            )
            return
        if status not in {"completed", "failed", "skipped"}:
            return
        step_state = ((session.get("pipeline") or {}).get("steps") or {}).get(step) or {}
        context = log_context(
            session_id,
            step,
            status=status,
            runtime_seconds=step_state.get("runtimeSeconds"),
        )
        if status == "failed":
            logger.error("Pipeline step failed: %s (%s)", step, error, extra=context)
        else:
            logger.info("Pipeline step %s: %s", status, step, extra=context)

    def _set_pipeline_step_state(
        self,
        session: dict[str, Any],
        step: str,
        status: str,
        *,
        metadata: dict[str, Any] | None = None,
        error: Exception | str | None = None,
    ) -> None:
        now = self.now_iso()
        pipeline = session.setdefault("pipeline", {})
        steps = pipeline.setdefault("steps", {})
        current = dict(steps.get(step) or {})
        current["status"] = status
        current["updatedAt"] = now

        if status == "running":
            current.setdefault("startedAt", now)
            current.pop("endedAt", None)
            current.pop("runtimeSeconds", None)
            pipeline["currentStep"] = step
        elif status in {"completed", "failed", "skipped"}:
            current.setdefault("startedAt", now)
            current["endedAt"] = now
            current["runtimeSeconds"] = self.runtime_seconds(str(current["startedAt"]), now)
            if pipeline.get("currentStep") == step:
                pipeline["currentStep"] = None

        if metadata is not None:
            current_metadata = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
            current_metadata.update(metadata)
            current["metadata"] = current_metadata

        if error is not None:
            current["error"] = str(error) or type(error).__name__
        elif status in {"running", "completed", "skipped"}:
            current.pop("error", None)

        steps[step] = current

    @staticmethod
    def _audio_output_metadata(session_id: str, audio_path: Path) -> dict[str, Any]:
        stats = audio_path.stat()
        return {
            "fileName": audio_path.name,
            "absolutePath": str(audio_path),
            "url": f"/media/audio/{audio_path.name}",
            "sizeBytes": stats.st_size,
        }

    def _expected_json_output_path(self, session_id: str, key: str) -> Path | None:
        if key == "audioProfessionalism":
            return self.media.settings.paths.output_audio_professionalism_dir / f"{session_id}.json"
        if key == "communicationScores":
            return self.media.settings.paths.output_communication_scores_dir / f"{session_id}.json"
        if key == "scores":
            return self.media.settings.paths.output_scores_dir / f"{session_id}.json"
        return None

    @staticmethod
    def _json_output_metadata(key: str, path: Path) -> dict[str, Any]:
        url_by_key = {
            "audioProfessionalism": "/media/audio-professionalism",
            "communicationScores": "/media/communication-scores",
            "scores": "/media/scores",
        }
        stats = path.stat()
        return {
            "fileName": path.name,
            "absolutePath": str(path),
            "url": f"{url_by_key[key]}/{path.name}",
            "sizeBytes": stats.st_size,
        }

    async def _discover_existing_json_output(
        self,
        session: dict[str, Any],
        key: str,
        predicate,
    ) -> dict[str, Any] | None:
        path = self._expected_json_output_path(str(session["id"]), key)
        if path is None or not path.exists():
            return None
        try:
            raw = await self._read_text(path)
            payload = extract_json_object(raw)
            if predicate(payload):
                return None
        except Exception:
            logger.debug(
                "Could not load existing %s output; treating as absent.",
                key,
                exc_info=True,
                extra=log_context(str(session.get("id") or ""), "output_discovery", output_key=key),
            )
            return None

        output = self._json_output_metadata(key, path)
        output["payload"] = payload
        return output

    @staticmethod
    def _predicate_or_default(scoring: Any, name: str):
        predicate = getattr(scoring, name, None)
        if callable(predicate):
            return predicate
        return lambda payload: payload is None

    async def _persist_session_output(
        self,
        session: dict[str, Any],
        key: str,
        value: dict[str, Any] | None,
        *,
        lock: asyncio.Lock | None = None,
    ) -> None:
        if lock is None:
            session.setdefault("outputs", {})[key] = value
            await self.sessions.write(session)
            return

        async with lock:
            session.setdefault("outputs", {})[key] = value
            await self.sessions.write(session)

    async def _load_output_payload(
        self,
        output: dict[str, Any] | None,
    ) -> tuple[dict[str, Any] | None, Any, bool]:
        if not isinstance(output, dict):
            return None, None, False

        raw = await self._read_text_if_exists(self._output_absolute_path(output))
        if raw:
            payload = extract_json_object(raw)
        else:
            payload = output.get("payload")

        if payload is None:
            return output, None, False
        if output.get("payload") is not None:
            return output, payload, False

        updated_output = dict(output)
        updated_output["payload"] = payload
        return updated_output, payload, True

    async def _run_cached_scoring_branches(self, session: dict[str, Any]) -> dict[str, Any]:
        if not self.media.settings.parallel_scoring:
            return await self._run_cached_scoring_branches_sequential(session)
        return await self._run_cached_scoring_branches_parallel(session)

    async def _run_cached_scoring_branches_sequential(self, session: dict[str, Any]) -> dict[str, Any]:
        audio_result = await self._refresh_or_load_audio_professionalism(session)
        communication_result = await self._refresh_or_load_communication_scores(
            session,
            audio_result.get("output"),
        )
        content_result = await self._refresh_or_load_content_scores(session)
        return {
            "audioProfessionalism": audio_result.get("payload"),
            "communicationScores": communication_result.get("payload"),
            "scores": content_result.get("payload"),
        }

    async def _run_cached_scoring_branches_parallel(self, session: dict[str, Any]) -> dict[str, Any]:
        output_lock = asyncio.Lock()

        async def communication_branch() -> dict[str, Any]:
            branch: dict[str, Any] = {
                "audioProfessionalism": None,
                "communicationScores": None,
                "error": None,
            }
            try:
                audio_result = await self._refresh_or_load_audio_professionalism(session, lock=output_lock)
                branch["audioProfessionalism"] = audio_result.get("payload")
                communication_result = await self._refresh_or_load_communication_scores(
                    session,
                    audio_result.get("output"),
                    lock=output_lock,
                )
                branch["communicationScores"] = communication_result.get("payload")
            except Exception as error:
                branch["error"] = error
            return branch

        async def content_branch() -> dict[str, Any]:
            branch: dict[str, Any] = {"scores": None, "error": None}
            try:
                content_result = await self._refresh_or_load_content_scores(session, lock=output_lock)
                branch["scores"] = content_result.get("payload")
            except Exception as error:
                branch["error"] = error
            return branch

        communication_result, content_result = await asyncio.gather(
            communication_branch(),
            content_branch(),
        )

        errors = [
            error
            for error in (communication_result.get("error"), content_result.get("error"))
            if isinstance(error, Exception)
        ]
        if errors:
            messages = "; ".join(str(error) or type(error).__name__ for error in errors)
            raise RuntimeError(f"Post-transcription scoring failed: {messages}") from errors[0]

        return {
            "audioProfessionalism": communication_result.get("audioProfessionalism"),
            "communicationScores": communication_result.get("communicationScores"),
            "scores": content_result.get("scores"),
        }

    async def _refresh_or_load_audio_professionalism(
        self,
        session: dict[str, Any],
        *,
        lock: asyncio.Lock | None = None,
    ) -> dict[str, Any]:
        session_id = str(session["id"])
        if not self.media.settings.enable_audio_professionalism:
            await self._mark_pipeline_step(session, "audio_professionalism", "skipped", lock=lock)
            await self.events.publish(
                session_id,
                "milestone",
                {"code": "audio_professionalism_complete", "message": "audio professionalism disabled"},
            )
            return {"output": None, "payload": None}

        predicate = self._predicate_or_default(self.scoring, "should_refresh_audio_professionalism_payload")
        output = (session.get("outputs") or {}).get("audioProfessionalism")
        if not isinstance(output, dict):
            output = await self._discover_existing_json_output(session, "audioProfessionalism", predicate)
            if output is not None:
                await self._persist_session_output(session, "audioProfessionalism", output, lock=lock)
        existing = self._output_absolute_path(output)
        needs_refresh = await self._payload_needs_refresh(
            existing,
            predicate,
        )
        if needs_refresh:
            await self._mark_pipeline_step(session, "audio_professionalism", "running", lock=lock)
            await self.events.publish(
                session_id,
                "milestone",
                {
                    "code": "audio_professionalism_started",
                    "message": "refreshing audio professionalism" if existing else "extracting audio professionalism",
                },
            )
            try:
                output = await self.scoring.run_audio_professionalism(session)
            except Exception as error:
                await self._persist_session_output(session, "audioProfessionalism", None, lock=lock)
                await self._mark_pipeline_step(session, "audio_professionalism", "failed", error=error, lock=lock)
                raise
            await self._persist_session_output(session, "audioProfessionalism", output, lock=lock)
            await self._mark_pipeline_step(
                session,
                "audio_professionalism",
                "completed",
                metadata={"reusedExistingArtifact": False},
                lock=lock,
            )
            await self.events.publish(
                session_id,
                "milestone",
                {"code": "audio_professionalism_complete", "message": "audio professionalism ready"},
            )
        else:
            await self._mark_pipeline_step(
                session,
                "audio_professionalism",
                "completed",
                metadata={"reusedExistingArtifact": True},
                lock=lock,
            )

        output, payload, updated = await self._load_output_payload(output)
        if updated:
            await self._persist_session_output(session, "audioProfessionalism", output, lock=lock)
        return {"output": output, "payload": payload}

    async def _refresh_or_load_communication_scores(
        self,
        session: dict[str, Any],
        audio_professionalism: dict[str, Any] | None,
        *,
        lock: asyncio.Lock | None = None,
    ) -> dict[str, Any]:
        session_id = str(session["id"])
        if not self.media.settings.enable_communication_scoring:
            await self._mark_pipeline_step(session, "communication_scoring", "skipped", lock=lock)
            await self.events.publish(
                session_id,
                "milestone",
                {"code": "communication_scoring_complete", "message": "communication scoring disabled"},
            )
            return {"output": None, "payload": None}

        predicate = self._predicate_or_default(self.scoring, "should_refresh_communication_payload")
        output = (session.get("outputs") or {}).get("communicationScores")
        if not isinstance(output, dict):
            output = await self._discover_existing_json_output(session, "communicationScores", predicate)
            if output is not None:
                await self._persist_session_output(session, "communicationScores", output, lock=lock)
        existing = self._output_absolute_path(output)
        needs_refresh = await self._payload_needs_refresh(existing, predicate)
        if needs_refresh:
            await self._mark_pipeline_step(session, "communication_scoring", "running", lock=lock)
            await self.events.publish(
                session_id,
                "milestone",
                {
                    "code": "communication_scoring_started",
                    "message": "refreshing communication scores" if existing else "running communication scoring",
                },
            )
            try:
                output = await self.scoring.run_communication_scoring(session, audio_professionalism)
            except Exception as error:
                await self._persist_session_output(session, "communicationScores", None, lock=lock)
                await self._mark_pipeline_step(session, "communication_scoring", "failed", error=error, lock=lock)
                raise
            await self._persist_session_output(session, "communicationScores", output, lock=lock)
            await self._mark_pipeline_step(
                session,
                "communication_scoring",
                "completed",
                metadata={"reusedExistingArtifact": False},
                lock=lock,
            )
            await self.events.publish(
                session_id,
                "milestone",
                {"code": "communication_scoring_complete", "message": "communication scores ready"},
            )
        else:
            await self._mark_pipeline_step(
                session,
                "communication_scoring",
                "completed",
                metadata={"reusedExistingArtifact": True},
                lock=lock,
            )

        output, payload, updated = await self._load_output_payload(output)
        if updated:
            await self._persist_session_output(session, "communicationScores", output, lock=lock)
        return {"output": output, "payload": payload}

    async def _refresh_or_load_content_scores(
        self,
        session: dict[str, Any],
        *,
        lock: asyncio.Lock | None = None,
    ) -> dict[str, Any]:
        session_id = str(session["id"])
        if not self.media.settings.enable_scoring:
            await self._mark_pipeline_step(session, "content_scoring", "skipped", lock=lock)
            await self.events.publish(session_id, "milestone", {"code": "scored", "message": "scoring disabled"})
            return {"output": None, "payload": None}

        predicate = self._predicate_or_default(self.scoring, "should_refresh_score_payload")
        output = (session.get("outputs") or {}).get("scores")
        if not isinstance(output, dict):
            output = await self._discover_existing_json_output(session, "scores", predicate)
            if output is not None:
                await self._persist_session_output(session, "scores", output, lock=lock)
        existing = self._output_absolute_path(output)
        needs_refresh = await self._payload_needs_refresh(existing, predicate)
        if needs_refresh:
            await self._mark_pipeline_step(session, "content_scoring", "running", lock=lock)
            await self.events.publish(
                session_id,
                "milestone",
                {"code": "transcription_complete", "message": "transcription/diarization already available"},
            )
            await self.events.publish(
                session_id,
                "milestone",
                {
                    "code": "scoring_started",
                    "message": "refreshing AI scoring output" if existing else "running AI scoring",
                },
            )
            try:
                output = await self.scoring.run_content_scoring(session)
            except Exception as error:
                await self._persist_session_output(session, "scores", None, lock=lock)
                await self._mark_pipeline_step(session, "content_scoring", "failed", error=error, lock=lock)
                raise
            await self._persist_session_output(session, "scores", output, lock=lock)
            await self._mark_pipeline_step(
                session,
                "content_scoring",
                "completed",
                metadata={"reusedExistingArtifact": False},
                lock=lock,
            )
            await self.events.publish(session_id, "milestone", {"code": "scored", "message": "scored"})
        else:
            await self._mark_pipeline_step(
                session,
                "content_scoring",
                "completed",
                metadata={"reusedExistingArtifact": True},
                lock=lock,
            )

        output, payload, updated = await self._load_output_payload(output)
        if updated:
            await self._persist_session_output(session, "scores", output, lock=lock)
        return {"output": output, "payload": payload}

    async def _run_fresh_scoring_branches(self, session: dict[str, Any]) -> dict[str, Any]:
        return await self._run_cached_scoring_branches(session)

    @staticmethod
    def _output_absolute_path(output: Any) -> str | None:
        if not isinstance(output, dict):
            return None
        value = output.get("absolutePath")
        return str(value) if value else None

    async def _payload_needs_refresh(self, absolute_path: str | None, predicate) -> bool:
        if not absolute_path or not Path(str(absolute_path)).exists():
            return True
        try:
            raw = await self._read_text(Path(str(absolute_path)))
            return bool(predicate(extract_json_object(raw)))
        except Exception:
            logger.debug(
                "Could not read cached output for refresh check; forcing recompute.",
                exc_info=True,
                extra=log_context("", "payload_refresh_check", path=str(absolute_path)),
            )
            return True

    async def _read_text_if_exists(self, absolute_path: str | None) -> str | None:
        if not absolute_path:
            return None
        path = Path(str(absolute_path))
        if not path.exists():
            return None
        return await self._read_text(path)

    @staticmethod
    async def _read_text(path: Path) -> str:
        return await asyncio.to_thread(path.read_text, encoding="utf-8")

    @staticmethod
    async def _read_json(path: Path) -> dict[str, Any]:
        raw = await PipelineService._read_text(path)
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError(f"Expected JSON object in {path}")
        return parsed

    now_iso = staticmethod(utc_now_iso)

    @staticmethod
    def runtime_seconds(started_at: str, ended_at: str) -> float:
        try:
            start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            end = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
            return round(max(0.0, (end - start).total_seconds()), 2)
        except Exception:
            return 0.0

    @staticmethod
    def _exception_message(error: Exception, fallback: str) -> str:
        return str(error) or type(error).__name__ or fallback
