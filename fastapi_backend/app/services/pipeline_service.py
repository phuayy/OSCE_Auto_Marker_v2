from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.artifacts import artifact_metadata, read_artifact_payload
from app.core.exceptions import AppError, EmptyTranscriptError
from app.core.json_utils import extract_json_object, write_json_file
from app.core.logging_utils import log_context
from app.core.utils import exception_message, runtime_seconds, utc_now_iso
from app.domain.enums import OutputKey, PipelineStep, StepStatus
from app.domain.notifications import NotificationType
from app.domain.outputs import OUTPUT_SPECS, OutputSpec
from app.domain.session_lifecycle import fail_session, record_progress, sync_current_step
from app.domain.sessions import SessionStatus
from app.pipeline.hallucination_filter import screen_segments
from app.pipeline.llm_preprocess import (
    TranscriptPreprocessor,
    diff_replacements,
    merge_corrected_segments,
)
from app.pipeline.media import MediaPipeline
from app.pipeline.scoring import ScoringPipeline
from app.pipeline.transcript_correction import (
    CorrectionPolicy,
    apply_replacements_to_file,
    correct_segments,
)
from app.pipeline.transcription.base import TranscriptionResult
from app.pipeline.transcription.registry import EngineDependencies
from app.repositories.app_settings_repository import AppSettingsRepository
from app.services.assessment_service import AssessmentService
from app.services.event_service import EventService
from app.services.session_service import SessionMutator, SessionService
from app.services.transcription_router import TranscriptionRouter

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from app.services.notification_service import NotificationService


logger = logging.getLogger(__name__)

# Pipeline step key for transcription. Named for the job rather than the tool
# because the engine behind it is operator-selectable; sessions recorded before
# the router shipped carry the older "whisperx" key, which the frontend stage
# gauge still recognises.
TRANSCRIPTION_STEP = PipelineStep.TRANSCRIPTION


def _assign_output(key: str, value: Any) -> SessionMutator:
    """Mutator: ``outputs[key] = value`` on whatever document is current."""

    def mutate(session: dict[str, Any]) -> Any:
        session.setdefault("outputs", {})[key] = value
        return None

    return mutate


def _merge_outputs(patch: dict[str, Any]) -> SessionMutator:
    def mutate(session: dict[str, Any]) -> Any:
        session.setdefault("outputs", {}).update(patch)
        return None

    return mutate


async def _scoring_disabled() -> dict[str, Any]:  # pragma: no cover - never called
    """Placeholder run for a disabled step; ``_refresh_or_load_output`` returns
    before it could be awaited."""
    raise AssertionError("content scoring is disabled")


class PipelineService:
    def __init__(
        self,
        sessions: SessionService,
        events: EventService,
        media: MediaPipeline,
        scoring: ScoringPipeline,
        assessments: AssessmentService | None = None,
        notifications: "NotificationService | None" = None,
        preprocessor: TranscriptPreprocessor | None = None,
        app_settings: AppSettingsRepository | None = None,
        transcription: TranscriptionRouter | None = None,
    ) -> None:
        self.sessions = sessions
        self.events = events
        self.media = media
        self.scoring = scoring
        self.assessments = assessments
        self.notifications = notifications
        self.preprocessor = preprocessor
        self.app_settings = app_settings
        self.transcription = transcription or self._default_transcription_router(events, media, app_settings)

    @staticmethod
    def _default_transcription_router(
        events: EventService,
        media: MediaPipeline,
        app_settings: AppSettingsRepository | None,
    ) -> TranscriptionRouter | None:
        """Assemble a router from the media pipeline when the caller supplied none.

        Production always injects one from the container; this keeps every
        other construction site (and the scoring-only test doubles) working. A
        media double that carries no runner or auth cannot transcribe anyway,
        so it simply gets no router.
        """
        runner = getattr(media, "runner", None)
        auth = getattr(media, "auth", None)
        settings = getattr(media, "settings", None)
        if runner is None or auth is None or settings is None:
            return None
        return TranscriptionRouter(
            settings,
            events,
            EngineDependencies(settings, runner, events, auth, media),
            app_settings=app_settings,
        )

    def _describe_transcription(self, result: TranscriptionResult) -> dict[str, Any]:
        """Engine provenance recorded on the session and the pipeline step."""
        engine = self.transcription.engines.get(result.engine_id) if self.transcription else None
        label = engine.descriptor.label if engine is not None else result.engine_id
        return TranscriptionRouter.result_metadata(result, label)

    async def _notify_scoring_complete(self, session: dict[str, Any]) -> None:
        if self.notifications is None:
            return
        name = str(session.get("name") or session.get("id"))
        await self.notifications.emit(
            NotificationType.SCORING_COMPLETED,
            "Scoring complete",
            f'Scoring is complete for "{name}". Results are ready to review.',
            session_id=str(session["id"]),
        )

    async def _notify_session_failed(self, session_id: str, message: str, failed_step: str) -> None:
        """Announce a failed run.

        Without this a failure is only visible to someone already looking at the
        session list — the very thing push notifications exist to avoid. Kept
        best-effort: this runs inside the failure handler, so raising here would
        mask the original error.
        """
        if self.notifications is None:
            return
        try:
            name = await self._session_display_name(session_id)
            await self.notifications.emit(
                NotificationType.SESSION_FAILED,
                "Processing failed",
                f'"{name}" failed at {failed_step}: {message}',
                session_id=session_id,
            )
        except Exception:
            logger.exception("Could not raise failure notification for session %s.", session_id)

    async def _session_display_name(self, session_id: str) -> str:
        try:
            session = await self.sessions.read(session_id)
        except Exception:
            return session_id
        return str(session.get("name") or session_id)

    async def mark_session_failed(self, session_id: str, error: Exception) -> None:
        message = self._exception_message(error, "Processing failed.")
        failed_step = "unknown"
        found: dict[str, str] = {}

        def mutate(session: dict[str, Any]) -> Any:
            found["step"] = self._find_failed_step(session) or "unknown"
            return fail_session(session, message)

        try:
            await self.sessions.update(session_id, mutate)
            failed_step = found.get("step") or "unknown"
        except Exception:
            logger.exception("Failed to persist failed session state for session %s.", session_id)
        # Single, searchable line naming the step the pipeline failed at.
        logger.error(
            "Session processing failed at step '%s': %s",
            failed_step,
            message,
            extra=log_context(session_id, failed_step, status=StepStatus.FAILED),
        )
        await self.events.publish(
            session_id,
            "status",
            {"code": StepStatus.FAILED, "message": message, "failedStep": failed_step},
        )
        await self._notify_session_failed(session_id, message, failed_step)

    @staticmethod
    def _find_failed_step(session: dict[str, Any]) -> str | None:
        """Identify the step a run failed at: the most recently-updated failed
        step, falling back to the pipeline's current step."""
        pipeline = session.get("pipeline") or {}
        steps = pipeline.get("steps") if isinstance(pipeline.get("steps"), dict) else {}
        failed = [
            (str(name), str((state or {}).get("updatedAt") or ""))
            for name, state in steps.items()
            if isinstance(state, dict) and state.get("status") == StepStatus.FAILED
        ]
        if failed:
            failed.sort(key=lambda item: item[1], reverse=True)
            return failed[0][0]
        current = pipeline.get("currentStep")
        return str(current) if current else None

    async def process_session_by_id(self, session_id: str, *, allow_processing: bool = False) -> dict[str, Any]:
        session = await self.sessions.read(session_id)
        if session.get("status") == SessionStatus.PROCESSING and not allow_processing:
            raise AppError("This session is already being processed.", status_code=409)

        if self._has_cached_transcript_artifact(session):
            return await self._process_cached_transcript(session)
        return await self._process_from_video(session)

    @staticmethod
    def _has_cached_transcript_artifact(session: dict[str, Any]) -> bool:
        """True when the session's recorded transcript still exists on disk.

        The recorded path alone is not proof the artifact is there: it can be
        removed out from under a session by manual cleanup, a wiped storage
        volume, or a partially-applied re-run. Branching on the string alone
        sent every subsequent run down the cached path, where the first read
        raised ``FileNotFoundError`` and the session could never recover. A
        dangling reference is dropped from ``outputs`` so the caller falls back
        to full transcription and the session record stops advertising an
        artifact that is gone.
        """
        transcript = (session.get("outputs") or {}).get(OutputKey.TRANSCRIPT)
        raw_path = transcript.get("absolutePath") if isinstance(transcript, dict) else None
        if not raw_path:
            return False
        if Path(str(raw_path)).is_file():
            return True
        logger.warning(
            "Recorded transcript artifact is missing from disk; falling back to full transcription.",
            extra=log_context(str(session.get("id") or ""), "transcript_cache_check", path=str(raw_path)),
        )
        session.setdefault("outputs", {})[OutputKey.TRANSCRIPT] = None
        return False

    @staticmethod
    def _assert_transcript_has_segments(transcript: dict[str, Any], source: str) -> None:
        """Fail the run when ``transcript`` holds no speech segments.

        Every scorer reads only the transcript, so an empty one does not produce
        an empty result — it produces a full, confident-looking assessment of
        nothing. This is the last gate before the scoring branches, and it
        guards both the fresh and cached paths.
        """
        segments = transcript.get("segments")
        if isinstance(segments, list) and segments:
            return
        raise EmptyTranscriptError(
            f"{source} contains no usable speech segments, so there is nothing to score. "
            "Check that the recording contains audible speech and that audio extraction succeeded."
        )

    async def _process_cached_transcript(self, session: dict[str, Any]) -> dict[str, Any]:
        session_id = str(session["id"])
        if await self.media.ensure_session_subtitle_track(session):
            await self._commit(session, _assign_output(OutputKey.SUBTITLE_TRACK, session["outputs"][OutputKey.SUBTITLE_TRACK]))
        transcript_path = Path(str(session["outputs"][OutputKey.TRANSCRIPT]["absolutePath"]))
        transcript = await self._read_json(transcript_path)
        self._assert_transcript_has_segments(transcript, f"Cached transcript {transcript_path.name}")

        if self.media.settings.enable_audio_professionalism:
            await self._ensure_audio_output(session)

        scoring_outputs = await self._run_cached_scoring_branches(session)

        # Results are durable before the session says so: a persistence failure
        # fails the run instead of flipping a session (and its webhook) from
        # "completed" back to "failed".
        await self._record_assessment_results(session)
        already_completed = session.get("status") == SessionStatus.COMPLETED
        if not already_completed:
            await self._commit(session, self._complete_mutator())
            await self._notify_scoring_complete(session)

        return {
            "session": self.sessions.public_session(session),
            OutputKey.TRANSCRIPT: transcript,
            OutputKey.AUDIO_PROFESSIONALISM: scoring_outputs.get(OutputKey.AUDIO_PROFESSIONALISM),
            OutputKey.COMMUNICATION_SCORES: scoring_outputs.get(OutputKey.COMMUNICATION_SCORES),
            OutputKey.SCORES: scoring_outputs.get(OutputKey.SCORES),
        }

    async def _screen_hallucinations(
        self,
        session: dict[str, Any],
        transcript: dict[str, Any],
    ) -> None:
        """Check the classic Whisper hallucination signature on the normalized
        transcript and record every hit in ``transcript["hallucinations"]``.

        Runs before corpus correction so a hallucinated segment is never
        "corrected" into a plausible-looking clinical sentence. Best-effort by
        the same policy as the corrections: a screening failure never fails the
        pipeline, and the unscreened transcript proceeds."""
        settings = self.media.settings
        transcript["hallucinations"] = []
        if not settings.transcript_hallucination_filter:
            return
        try:
            kept, flags = screen_segments(
                transcript.get("segments") or [],
                drop=settings.transcript_hallucination_drop,
                min_avg_logprob=settings.transcript_hallucination_min_avg_logprob,
                max_compression_ratio=settings.transcript_hallucination_max_compression_ratio,
                max_ngram_repeats_allowed=settings.transcript_hallucination_max_ngram_repeats,
            )
            transcript["hallucinations"] = flags
            if not flags:
                return
            transcript["segments"] = kept
            transcript["segmentCount"] = len(kept)
            dropped = sum(1 for flag in flags if flag.get("action") == "dropped")
            await self.events.publish(
                str(session["id"]),
                "log",
                {
                    "source": "hallucination-filter",
                    "message": (
                        f"Flagged {len(flags)} suspected hallucinated segment(s); "
                        f"{dropped} removed before scoring."
                    ),
                },
            )
        except Exception:
            logger.exception(
                "Hallucination screening failed for session %s; using the unscreened transcript.",
                session.get("id"),
            )

    def _correction_policy(self) -> CorrectionPolicy:
        """Corpus-correction thresholds from Settings, read per run."""
        settings = self.media.settings
        return CorrectionPolicy(
            min_ratio=settings.transcript_correction_min_ratio,
            phonetic_enabled=settings.transcript_correction_phonetic,
            min_phonetic_ratio=settings.transcript_correction_min_phonetic_ratio,
            min_phonetic_char_ratio=settings.transcript_correction_min_phonetic_char_ratio,
            max_extra_span_words=settings.transcript_correction_max_extra_span_words,
        )

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
                self._correction_policy(),
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

    async def _run_llm_preprocess(
        self,
        session: dict[str, Any],
        transcript: dict[str, Any],
        whisperx_outputs: dict[str, Any],
        transcript_path: Path,
    ) -> None:
        """Optional Nemotron cleanup of the normalized transcript before any
        scorer reads it. Gated by the global llmTranscriptPreprocess setting,
        read live from the DB so a toggle applies to every subsequent run —
        clip children and Hatchet workers included. Best-effort: a failure is
        recorded on the step but never fails the run; scoring proceeds with
        the uncorrected transcript (same policy as corpus corrections)."""
        step = PipelineStep.LLM_PREPROCESS
        enabled = (
            self.preprocessor is not None
            and self.app_settings is not None
            and await self.app_settings.llm_preprocess_enabled()
        )
        if not enabled:
            await self._mark_pipeline_step(session, step, StepStatus.SKIPPED)
            return
        await self._mark_pipeline_step(session, step, StepStatus.RUNNING)
        try:
            payload = await self.preprocessor.run(session, transcript_path)
            changes = merge_corrected_segments(transcript, payload.get("segments") or [])
            transcript["llmPreprocess"] = {
                "applied": True,
                "model": payload.get("model"),
                "changes": changes,
            }
            # Rewrite even with zero changes: the llmPreprocess report block is
            # part of the transcript artifact.
            await asyncio.to_thread(write_json_file, transcript_path, transcript)
            size_bytes = transcript_path.stat().st_size

            def record_size(current: dict[str, Any]) -> Any:
                info = (current.get("outputs") or {}).get(OutputKey.TRANSCRIPT)
                if not isinstance(info, dict) or info.get("sizeBytes") == size_bytes:
                    return False
                info["sizeBytes"] = size_bytes
                return None

            await self._commit(session, record_size)
            subtitle_replacements = [
                replacement
                for change in changes
                for replacement in diff_replacements(change["original"], change["corrected"])
            ]
            if subtitle_replacements:
                for output_key in ("srtAbsolutePath", "vttAbsolutePath"):
                    subtitle_path = whisperx_outputs.get(output_key)
                    if subtitle_path:
                        await asyncio.to_thread(
                            apply_replacements_to_file, Path(str(subtitle_path)), subtitle_replacements
                        )
            await self.events.publish(
                str(session["id"]),
                "log",
                {
                    "source": "llm-preprocess",
                    "message": f"LLM transcript preprocess corrected {len(changes)} segment(s).",
                },
            )
            await self._mark_pipeline_step(session, step, StepStatus.COMPLETED, metadata={"changedSegments": len(changes)})
        except Exception as error:
            logger.exception(
                "LLM transcript preprocess failed for session %s; scoring uses the uncorrected transcript.",
                session.get("id"),
            )
            await self._mark_pipeline_step(session, step, StepStatus.FAILED, error=error)

    async def _process_from_video(self, session: dict[str, Any]) -> dict[str, Any]:
        session_id = str(session["id"])
        started_at = str((session.get("pipeline") or {}).get("startedAt") or self.now_iso())
        await self.events.publish(session_id, "milestone", {"code": "started", "message": "started"})

        def start(current: dict[str, Any]) -> Any:
            current["status"] = SessionStatus.PROCESSING
            current["error"] = None
            pipeline = current.setdefault("pipeline", {})
            pipeline["startedAt"] = started_at
            pipeline["endedAt"] = None
            pipeline["runtimeSeconds"] = None
            return None

        await self._commit(session, start)

        audio_info = await self._ensure_audio_output(session)

        if self.transcription is None:
            raise AppError("No transcription engine is configured for this pipeline.", status_code=500)

        await self._mark_pipeline_step(session, TRANSCRIPTION_STEP, StepStatus.RUNNING)
        # Transcription is the longest step by far, and the only one that
        # reports its own progress; the lock guards the session document
        # against the output callbacks that carry those readings.
        progress_lock = asyncio.Lock()
        try:
            transcription = await self.transcription.transcribe(
                session,
                audio_info,
                on_progress=lambda percent: self._record_step_progress(
                    session, TRANSCRIPTION_STEP, percent, lock=progress_lock
                ),
            )
        except Exception as error:
            await self._mark_pipeline_step(session, TRANSCRIPTION_STEP, StepStatus.FAILED, error=error)
            raise
        whisperx_outputs = transcription.to_outputs()
        engine_metadata = self._describe_transcription(transcription)
        await self._commit(session, lambda current: current.__setitem__(PipelineStep.TRANSCRIPTION, engine_metadata))
        await self._mark_pipeline_step(
            session, TRANSCRIPTION_STEP, StepStatus.COMPLETED, metadata=engine_metadata
        )

        await self._mark_pipeline_step(session, PipelineStep.TRANSCRIPT_NORMALIZATION, StepStatus.RUNNING)
        whisperx_raw = await self._read_json(Path(str(whisperx_outputs["jsonAbsolutePath"])))
        normalized_transcript = self.media.normalize_whisperx_transcript(whisperx_raw)
        try:
            self._assert_transcript_has_segments(normalized_transcript, "The normalized transcript")
        except EmptyTranscriptError as error:
            await self._mark_pipeline_step(session, PipelineStep.TRANSCRIPT_NORMALIZATION, StepStatus.FAILED, error=error)
            raise
        await self._screen_hallucinations(session, normalized_transcript)
        await self._apply_corpus_corrections(session, normalized_transcript, whisperx_outputs)

        transcript_file_name = f"{session_id}.json"
        transcript_path = self.media.settings.paths.output_transcripts_dir / transcript_file_name
        await asyncio.to_thread(write_json_file, transcript_path, normalized_transcript)

        whisperx_stats = Path(str(whisperx_outputs["jsonAbsolutePath"])).stat()
        outputs_patch: dict[str, Any] = {
            OutputKey.WHISPERX_JSON: {
                "fileName": Path(str(whisperx_outputs["jsonAbsolutePath"])).name,
                "absolutePath": str(whisperx_outputs["jsonAbsolutePath"]),
                "sizeBytes": whisperx_stats.st_size,
            },
            OutputKey.SUBTITLE: None,
            OutputKey.SUBTITLE_TRACK: None,
            OutputKey.TRANSCRIPT: artifact_metadata(transcript_path, "/media/transcripts"),
        }
        srt_path = whisperx_outputs.get("srtAbsolutePath")
        if srt_path:
            outputs_patch[OutputKey.SUBTITLE] = artifact_metadata(Path(str(srt_path)), f"/media/whisperx/{session_id}")
        vtt_path = whisperx_outputs.get("vttAbsolutePath")
        if vtt_path:
            outputs_patch[OutputKey.SUBTITLE_TRACK] = artifact_metadata(Path(str(vtt_path)), f"/media/whisperx/{session_id}")
        await self._commit(session, _merge_outputs(outputs_patch))
        await self._mark_pipeline_step(session, PipelineStep.TRANSCRIPT_NORMALIZATION, StepStatus.COMPLETED)
        await self.events.publish(
            session_id,
            "milestone",
            {"code": "transcription_complete", "message": "transcription/diarization complete"},
        )

        await self._run_llm_preprocess(session, normalized_transcript, whisperx_outputs, transcript_path)

        scoring_outputs = await self._run_cached_scoring_branches(session)
        audio_prof_payload = scoring_outputs.get(OutputKey.AUDIO_PROFESSIONALISM)
        communication_payload = scoring_outputs.get(OutputKey.COMMUNICATION_SCORES)
        scoring_payload = scoring_outputs.get(OutputKey.SCORES)

        # Persist results first, then declare the session complete, then tell
        # the world — see _process_cached_transcript for why this order.
        await self._record_assessment_results(session)
        await self._commit(session, self._complete_mutator(started_at))
        await self._notify_scoring_complete(session)
        await self.events.publish(
            session_id,
            "status",
            {"code": StepStatus.COMPLETED, "message": "Processing completed successfully."},
        )
        return {
            "session": self.sessions.public_session(session),
            OutputKey.TRANSCRIPT: normalized_transcript,
            OutputKey.AUDIO_PROFESSIONALISM: audio_prof_payload,
            OutputKey.COMMUNICATION_SCORES: communication_payload,
            OutputKey.SCORES: scoring_payload,
        }

    def _complete_mutator(self, started_at: str | None = None) -> SessionMutator:
        """Mutator that closes a run as completed, computing the runtime from
        the stored ``startedAt`` (or ``started_at`` when the caller knows it)."""

        def mutate(session: dict[str, Any]) -> Any:
            pipeline = session.setdefault("pipeline", {})
            begun = started_at or pipeline.get("startedAt") or self.now_iso()
            ended = self.now_iso()
            pipeline["startedAt"] = begun
            pipeline["endedAt"] = ended
            pipeline["runtimeSeconds"] = self.runtime_seconds(str(begun), ended)
            session["status"] = SessionStatus.COMPLETED
            session["error"] = None
            return None

        return mutate

    async def _commit(self, session: dict[str, Any], mutate: SessionMutator) -> dict[str, Any]:
        """Apply ``mutate`` to the stored row and adopt the result into ``session``.

        The pipeline keeps one working dict per run for the paths and outputs
        it reads constantly, but it never writes that dict back. Every change is
        a mutator replayed on the *current* row (``SessionService.update``), so
        a rename, a job-status sync or a clip export landing mid-run is kept
        rather than overwritten by a copy loaded an hour earlier. The working
        dict is refreshed in place — same object, new contents — because the
        progress callbacks hold a reference to it.
        """
        fresh = await self.sessions.update(str(session["id"]), mutate)
        session.clear()
        session.update(fresh)
        return session

    async def _record_assessment_results(self, session: dict[str, Any]) -> None:
        if self.assessments is None:
            return
        await self._mark_pipeline_step(session, PipelineStep.ASSESSMENT_PERSISTENCE, StepStatus.RUNNING)
        try:
            await self.assessments.record_session_results(session)
        except Exception as error:
            await self._mark_pipeline_step(session, PipelineStep.ASSESSMENT_PERSISTENCE, StepStatus.FAILED, error=error)
            raise
        await self._mark_pipeline_step(session, PipelineStep.ASSESSMENT_PERSISTENCE, StepStatus.COMPLETED)

    async def _ensure_audio_output(self, session: dict[str, Any]) -> dict[str, Any]:
        audio_path_raw = ((session.get("outputs") or {}).get(OutputKey.AUDIO) or {}).get("absolutePath")
        session_id = str(session["id"])
        if audio_path_raw:
            audio_path = Path(str(audio_path_raw))
            if audio_path.exists() and audio_path.stat().st_size > 0:
                await self._mark_pipeline_step(
                    session,
                    PipelineStep.AUDIO_EXTRACTION,
                    StepStatus.COMPLETED,
                    metadata={"reusedExistingArtifact": True},
                )
                return dict(session["outputs"][OutputKey.AUDIO])

        deterministic_audio_path = self.media.settings.paths.output_audio_dir / f"{session_id}.mp3"
        if deterministic_audio_path.exists() and deterministic_audio_path.stat().st_size > 0:
            audio_info = self._audio_output_metadata(session_id, deterministic_audio_path)
            await self._commit(session, _assign_output(OutputKey.AUDIO, audio_info))
            await self._mark_pipeline_step(
                session,
                PipelineStep.AUDIO_EXTRACTION,
                StepStatus.COMPLETED,
                metadata={"reusedExistingArtifact": True},
            )
            await self.events.publish(
                session_id,
                "milestone",
                {"code": "converted_to_mp3", "message": f"reused existing mp3 ({audio_info['fileName']})"},
            )
            return audio_info

        await self._mark_pipeline_step(session, PipelineStep.AUDIO_EXTRACTION, StepStatus.RUNNING)
        try:
            audio_info = await self.media.extract_audio_to_mp3(session)
        except Exception as error:
            await self._mark_pipeline_step(session, PipelineStep.AUDIO_EXTRACTION, StepStatus.FAILED, error=error)
            raise
        await self._commit(session, _assign_output(OutputKey.AUDIO, audio_info))
        await self._mark_pipeline_step(
            session,
            PipelineStep.AUDIO_EXTRACTION,
            StepStatus.COMPLETED,
            metadata={"reusedExistingArtifact": False},
        )
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
        def mutate(current: dict[str, Any]) -> Any:
            self._set_pipeline_step_state(current, step, status, metadata=metadata, error=error)
            return None

        if lock is None:
            await self._commit(session, mutate)
        else:
            async with lock:
                await self._commit(session, mutate)
        self._log_pipeline_step(session, step, status, error=error)

    async def _record_step_progress(
        self,
        session: dict[str, Any],
        step: str,
        percent: float,
        *,
        lock: asyncio.Lock,
    ) -> None:
        """Persist a live completion percentage for a still-running step.

        The session-list projection reads ``pipeline.stepProgress``, so this is
        what turns the card's stage gauge from a fixed per-step fraction into a
        moving one. Output callbacks are dispatched from the subprocess reader
        threads, so the lock serialises what would otherwise be concurrent
        writers of the same session document. A reading that arrives after the
        step ended is dropped rather than resurrecting a finished step.
        """
        def mutate(current: dict[str, Any]) -> Any:
            return record_progress(current, step, percent, tracked_step=True)

        async with lock:
            await self._commit(session, mutate)

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
        if status == StepStatus.RUNNING:
            logger.info(
                "Pipeline step started: %s",
                step,
                extra=log_context(session_id, step, status=status),
            )
            return
        if status not in {StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.SKIPPED}:
            return
        step_state = ((session.get("pipeline") or {}).get("steps") or {}).get(step) or {}
        context = log_context(
            session_id,
            step,
            status=status,
            runtime_seconds=step_state.get("runtimeSeconds"),
        )
        if status == StepStatus.FAILED:
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
        step = PipelineStep(step)
        status = StepStatus(status)
        now = self.now_iso()
        pipeline = session.setdefault("pipeline", {})
        steps = pipeline.setdefault("steps", {})
        current = dict(steps.get(step) or {})
        current["status"] = status
        current["updatedAt"] = now

        if status == StepStatus.RUNNING:
            current.setdefault("startedAt", now)
            current.pop("endedAt", None)
            current.pop("runtimeSeconds", None)
            # A fresh step starts with no progress reading; a stale one from
            # the previous step would otherwise be projected onto this one.
            current.pop("progress", None)
        elif status in {StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.SKIPPED}:
            current.setdefault("startedAt", now)
            current["endedAt"] = now
            current["runtimeSeconds"] = self.runtime_seconds(str(current["startedAt"]), now)

        if metadata is not None:
            current_metadata = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
            current_metadata.update(metadata)
            current["metadata"] = current_metadata

        if error is not None:
            current["error"] = str(error) or type(error).__name__
        elif status in {StepStatus.RUNNING, StepStatus.COMPLETED, StepStatus.SKIPPED}:
            current.pop("error", None)

        steps[step] = current
        # currentStep/stepProgress are a projection of `steps`, never set by
        # hand: under PARALLEL_SCORING two branches can be running at once,
        # and deriving both scalars here — instead of each caller poking them
        # — is what keeps the card from ever pairing one step's name with
        # another step's percentage. See sync_current_step's docstring.
        sync_current_step(pipeline)

    @staticmethod
    def _audio_output_metadata(session_id: str, audio_path: Path) -> dict[str, Any]:
        return artifact_metadata(audio_path, "/media/audio")

    def _expected_json_output_path(self, session_id: str, key: str) -> Path | None:
        if key == OutputKey.AUDIO_PROFESSIONALISM:
            return self.media.settings.paths.output_audio_professionalism_dir / f"{session_id}.json"
        if key == OutputKey.COMMUNICATION_SCORES:
            return self.media.settings.paths.output_communication_scores_dir / f"{session_id}.json"
        if key == OutputKey.SCORES:
            return self.media.settings.paths.output_scores_dir / f"{session_id}.json"
        return None

    @staticmethod
    def _json_output_metadata(key: str, path: Path) -> dict[str, Any]:
        return artifact_metadata(path, OUTPUT_SPECS[key].media_directory)

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

        return self._json_output_metadata(key, path)

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
            await self._commit(session, _assign_output(key, value))
            return

        async with lock:
            await self._commit(session, _assign_output(key, value))

    async def _load_output_payload(
        self,
        output: dict[str, Any] | None,
    ) -> tuple[dict[str, Any] | None, Any, bool]:
        if not isinstance(output, dict):
            return None, None, False

        try:
            payload = await read_artifact_payload(output)
        except FileNotFoundError:
            return output, None, False
        updated_output = dict(output)
        raw_path = self._output_absolute_path(output)
        if raw_path and Path(raw_path).is_file():
            updated_output.pop("payload", None)
        return updated_output, payload, updated_output != output

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
            OutputKey.AUDIO_PROFESSIONALISM: audio_result.get("payload"),
            OutputKey.COMMUNICATION_SCORES: communication_result.get("payload"),
            OutputKey.SCORES: content_result.get("payload"),
        }

    async def _run_cached_scoring_branches_parallel(self, session: dict[str, Any]) -> dict[str, Any]:
        output_lock = asyncio.Lock()

        async def communication_branch() -> dict[str, Any]:
            branch: dict[str, Any] = {
                OutputKey.AUDIO_PROFESSIONALISM: None,
                OutputKey.COMMUNICATION_SCORES: None,
                "error": None,
            }
            try:
                audio_result = await self._refresh_or_load_audio_professionalism(session, lock=output_lock)
                branch[OutputKey.AUDIO_PROFESSIONALISM] = audio_result.get("payload")
                communication_result = await self._refresh_or_load_communication_scores(
                    session,
                    audio_result.get("output"),
                    lock=output_lock,
                )
                branch[OutputKey.COMMUNICATION_SCORES] = communication_result.get("payload")
            except Exception as error:
                branch["error"] = error
            return branch

        async def content_branch() -> dict[str, Any]:
            branch: dict[str, Any] = {OutputKey.SCORES: None, "error": None}
            try:
                content_result = await self._refresh_or_load_content_scores(session, lock=output_lock)
                branch[OutputKey.SCORES] = content_result.get("payload")
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
            OutputKey.AUDIO_PROFESSIONALISM: communication_result.get(OutputKey.AUDIO_PROFESSIONALISM),
            OutputKey.COMMUNICATION_SCORES: communication_result.get(OutputKey.COMMUNICATION_SCORES),
            OutputKey.SCORES: content_result.get(OutputKey.SCORES),
        }

    async def _refresh_or_load_audio_professionalism(
        self,
        session: dict[str, Any],
        *,
        lock: asyncio.Lock | None = None,
    ) -> dict[str, Any]:
        return await self._refresh_or_load_output(
            session, OUTPUT_SPECS[OutputKey.AUDIO_PROFESSIONALISM],
            enabled=self.media.settings.enable_audio_professionalism,
            run=lambda: self.scoring.run_audio_professionalism(session),
            predicate=self._predicate_or_default(self.scoring, "should_refresh_audio_professionalism_payload"),
            lock=lock,
        )

    async def _refresh_or_load_communication_scores(
        self,
        session: dict[str, Any],
        audio_professionalism: dict[str, Any] | None,
        *,
        lock: asyncio.Lock | None = None,
    ) -> dict[str, Any]:
        return await self._refresh_or_load_output(
            session, OUTPUT_SPECS[OutputKey.COMMUNICATION_SCORES],
            enabled=self.media.settings.enable_communication_scoring,
            run=lambda: self.scoring.run_communication_scoring(session, audio_professionalism),
            predicate=self._predicate_or_default(self.scoring, "should_refresh_communication_payload"),
            lock=lock,
        )

    async def _refresh_or_load_content_scores(
        self,
        session: dict[str, Any],
        *,
        lock: asyncio.Lock | None = None,
    ) -> dict[str, Any]:
        """Content scoring, under the marking mode the operator selected.

        The plan is resolved once, up front, so the cache check and the run
        agree about the mode — a single-model sheet on disk is refreshed when a
        panel was selected, and a panel's live progress reaches the session
        card. ``prepare_content_marking`` is required, not probed: the
        ``getattr`` fallback that used to stand in for it existed only so test
        doubles could skip implementing the seam, and a double that skips the
        seam is not exercising the path the pipeline actually takes
        (``tests/fixtures/scoring_doubles.py`` gives them the seam instead).
        """
        spec = OUTPUT_SPECS[OutputKey.SCORES]
        if not self.media.settings.enable_scoring:
            # Resolving a marking plan reads settings and the credential store.
            # A disabled step runs nothing, so it asks nothing: the plan is only
            # prepared once we know there is a run for it to describe.
            return await self._refresh_or_load_output(
                session, spec,
                enabled=False,
                run=_scoring_disabled,
                predicate=lambda _payload: False,
                lock=lock,
            )
        marking = await self.scoring.prepare_content_marking(session)
        # Progress writes share the branch lock when there is one; sequential
        # scoring has no concurrent writer, so a private lock only serialises
        # the subprocess reader threads against each other.
        progress_lock = lock or asyncio.Lock()

        async def on_progress(percent: float) -> None:
            await self._record_step_progress(session, spec.step, percent, lock=progress_lock)

        return await self._refresh_or_load_output(
            session, spec,
            enabled=self.media.settings.enable_scoring,
            run=lambda: marking.run(on_progress),
            predicate=marking.needs_refresh,
            lock=lock,
            step_metadata=marking.step_metadata,
        )

    async def _refresh_or_load_output(
        self,
        session: dict,
        spec: OutputSpec,
        *,
        enabled: bool,
        run: Callable[[], Awaitable[dict]],
        predicate: Callable[[object], bool],
        lock: asyncio.Lock | None,
        step_metadata: Callable[[], dict[str, Any]] | None = None,
    ) -> dict:
        """Reuse the output on disk when ``predicate`` accepts it, else ``run``.

        ``step_metadata`` is asked, after a fresh run, for anything the step
        should record beyond the reuse flag — how a panel's markers agreed, for
        one. It is a callable rather than a value because what there is to say
        is only known once the run has finished.
        """
        session_id = str(session["id"])
        if not enabled:
            await self._mark_pipeline_step(session, spec.step, StepStatus.SKIPPED, lock=lock)
            await self.events.publish(
                session_id, "milestone",
                {"code": spec.completed_event, "message": f"{spec.step} disabled"},
            )
            return {"output": None, "payload": None}
        output = (session.get("outputs") or {}).get(spec.key)
        if not isinstance(output, dict):
            output = await self._discover_existing_json_output(session, spec.key, predicate)
            if output is not None:
                await self._persist_session_output(session, spec.key, output, lock=lock)
        existing = self._output_absolute_path(output)
        needs_refresh = await self._payload_needs_refresh(existing, predicate)
        if needs_refresh:
            await self._mark_pipeline_step(session, spec.step, StepStatus.RUNNING, lock=lock)
            await self.events.publish(
                session_id,
                "milestone",
                {
                    "code": spec.started_event,
                    "message": f"{'Refreshing' if existing else 'Running'} {spec.step}",
                },
            )
            try:
                output = await run()
            except Exception as error:
                await self._persist_session_output(session, spec.key, None, lock=lock)
                await self._mark_pipeline_step(session, spec.step, StepStatus.FAILED, error=error, lock=lock)
                raise
            await self._persist_session_output(session, spec.key, output, lock=lock)
            metadata: dict[str, Any] = {"reusedExistingArtifact": False}
            if step_metadata is not None:
                metadata.update(step_metadata())
            await self._mark_pipeline_step(
                session,
                spec.step,
                StepStatus.COMPLETED,
                metadata=metadata,
                lock=lock,
            )
            await self.events.publish(
                session_id, "milestone", {"code": spec.completed_event, "message": f"{spec.step} ready"},
            )
        else:
            await self._mark_pipeline_step(
                session,
                spec.step,
                StepStatus.COMPLETED,
                metadata={"reusedExistingArtifact": True},
                lock=lock,
            )

        output, payload, updated = await self._load_output_payload(output)
        if updated:
            await self._persist_session_output(session, spec.key, output, lock=lock)
        return {"output": output, "payload": payload}

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

    runtime_seconds = staticmethod(runtime_seconds)
    _exception_message = staticmethod(exception_message)
