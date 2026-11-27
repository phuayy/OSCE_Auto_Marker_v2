"""WhisperX engine — the default, and the only one that does everything.

Implemented as an adapter over :class:`MediaPipeline`, which already owns the
WhisperX invocation, the filtered-WAV preparation, the artifact cache, the
CUDA fallback and the progress parsing. Wrapping rather than moving that code
keeps the engine boundary a pure addition: the WhisperX behaviour the pipeline
has been running is byte-for-byte the same behaviour it runs through the
router.
"""
from __future__ import annotations

import logging
from typing import Any

from app.pipeline.audio_filters import validate_filter_chain
from app.pipeline.media import MediaPipeline
from app.pipeline.whisperx_options import WhisperxRunOptions
from app.pipeline.transcription.base import (
    EngineAvailability,
    EngineCapabilities,
    EngineDescriptor,
    ParameterSpec,
    require_text,
    ParameterType,
    TranscriptionEngine,
    TranscriptionRequest,
    TranscriptionResult,
)

logger = logging.getLogger(__name__)

ENGINE_ID = "whisperx"

DESCRIPTOR = EngineDescriptor(
    id=ENGINE_ID,
    label="WhisperX",
    vendor="OpenAI Whisper + pyannote",
    description=(
        "Whisper transcription with wav2vec2 forced alignment and pyannote speaker "
        "diarisation. The only engine that labels speakers on its own, so it needs no "
        "second pass to tell the student from the simulated patient."
    ),
    capabilities=EngineCapabilities(
        diarization=True,
        word_timestamps=True,
        subtitles=True,
        progress=True,
    ),
    parameters=(
        ParameterSpec(
            name="model",
            label="Whisper model",
            type=ParameterType.STRING,
            default="large-v3",
            help="Checkpoint name. large-v3 is the accuracy default; distil-large-v3 and large-v3-turbo trade a little accuracy for speed.",
            # Names a HuggingFace repo or a local path the server loads.
            deployment_scoped=True,
            check=require_text,
        ),
        ParameterSpec(
            name="computeType",
            label="Compute type",
            type=ParameterType.ENUM,
            default="float16",
            options=("default", "float16", "float32", "int8"),
            help="float16 matches the native weight precision. Use int8 on cards with 4 GB or less; CPU runs downgrade automatically.",
        ),
        ParameterSpec(
            name="batchSize",
            label="Batch size",
            type=ParameterType.INTEGER,
            default=1,
            minimum=1,
            maximum=64,
            help="Chunks decoded at once. Raise on a GPU with more than 6 GB of VRAM.",
        ),
        ParameterSpec(
            name="chunkSize",
            label="VAD chunk size (seconds)",
            type=ParameterType.INTEGER,
            default=20,
            minimum=5,
            maximum=30,
            help="Speech merged into one decode. Shorter chunks span fewer speaker turns and give finer confidence granularity.",
        ),
        ParameterSpec(
            name="minSpeakers",
            label="Minimum speakers",
            type=ParameterType.INTEGER,
            default=2,
            minimum=0,
            maximum=10,
            help="0 lets the clustering estimate the count. An OSCE station normally records exactly two voices.",
        ),
        ParameterSpec(
            name="maxSpeakers",
            label="Maximum speakers",
            type=ParameterType.INTEGER,
            default=2,
            minimum=0,
            maximum=10,
            help="Raise to 3 for stations where an examiner also speaks.",
        ),
        ParameterSpec(
            name="audioFilters",
            label="ffmpeg filter chain",
            type=ParameterType.STRING,
            default="highpass=f=80,loudnorm",
            help="Applied to the dedicated 16 kHz WAV. Empty feeds the extracted MP3 unfiltered.",
            advanced=True,
            # An ffmpeg filter graph runs on the server; see audio_filters.
            deployment_scoped=True,
            check=validate_filter_chain,
        ),
        ParameterSpec(
            name="initialPrompt",
            label="Initial prompt",
            type=ParameterType.STRING,
            default="",
            help="Optional sentence that primes the register. Shares Whisper's 224-token prompt budget with the corpus hotwords.",
            advanced=True,
        ),
    ),
    requirements="Installed with the backend requirements; needs a HuggingFace token for pyannote diarisation.",
)


class WhisperXEngine(TranscriptionEngine):
    descriptor = DESCRIPTOR

    def __init__(self, media: MediaPipeline) -> None:
        self.media = media

    def default_options(self) -> dict[str, Any]:
        """Defaults come from ``Settings``, so the WHISPERX_* environment
        variables remain the deployment-wide baseline and the settings screen
        only ever layers per-deployment overrides on top of them."""
        settings = self.media.settings
        return {
            "model": settings.whisperx_model,
            "computeType": settings.whisperx_compute_type,
            "batchSize": settings.whisperx_batch_size,
            "chunkSize": settings.whisperx_chunk_size,
            "minSpeakers": settings.whisperx_min_speakers,
            "maxSpeakers": settings.whisperx_max_speakers,
            "audioFilters": settings.whisperx_audio_filters,
            "initialPrompt": settings.whisperx_initial_prompt,
        }

    async def availability(self) -> EngineAvailability:
        if not str(self.media.settings.whisperx_bin or "").strip():
            return EngineAvailability(False, "WHISPERX_BIN is not configured.")
        return EngineAvailability(True)

    @staticmethod
    def _speaker_bounds(options: dict[str, Any], deployment: WhisperxRunOptions) -> dict[str, int]:
        """The option's bounds, or none when the pair contradicts itself.

        0 means "let clustering estimate" and is respected. A minimum above a
        set maximum is an invalid combination; like any invalid option it
        degrades to the deployment default rather than failing the run.
        """
        minimum = options.get("minSpeakers")
        maximum = options.get("maxSpeakers")
        if minimum is not None and maximum is not None and 0 < maximum < minimum:
            logger.warning(
                "WhisperX minSpeakers (%s) exceeds maxSpeakers (%s); using the deployment defaults (%s/%s).",
                minimum,
                maximum,
                deployment.min_speakers,
                deployment.max_speakers,
            )
            return {}
        return {"min_speakers": minimum, "max_speakers": maximum}

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        options = request.options
        run_options = WhisperxRunOptions.from_settings(self.media.settings).with_overrides(
            model=options.get("model"),
            compute_type=options.get("computeType"),
            batch_size=options.get("batchSize"),
            chunk_size=options.get("chunkSize"),
            audio_filters=options.get("audioFilters"),
            initial_prompt=options.get("initialPrompt"),
            language=request.language or None,
        )
        # Speaker bounds: the account's option (already layered over the
        # WHISPERX_* defaults by ``resolve_options``) is the whole answer. The
        # request's own bounds are those same defaults and must not override it.
        run_options = run_options.with_overrides(**self._speaker_bounds(options, run_options))

        session = {
            "id": request.session_id,
            "corpus": {"terms": list(request.corpus_terms)},
        }
        audio_info = {
            "fileName": request.audio_file_name,
            "absolutePath": str(request.audio_path),
        }
        outputs = await self.media.run_whisperx_transcription(
            session,
            audio_info,
            on_progress=request.on_progress,
            options=run_options,
        )
        return TranscriptionResult(
            json_path=outputs["jsonAbsolutePath"],
            srt_path=outputs.get("srtAbsolutePath"),
            vtt_path=outputs.get("vttAbsolutePath"),
            engine_id=ENGINE_ID,
            model=run_options.model,
            diarized=True,
            metadata={
                "computeType": run_options.compute_type,
                "batchSize": run_options.batch_size,
                "chunkSize": run_options.chunk_size,
                "minSpeakers": run_options.min_speakers,
                "maxSpeakers": run_options.max_speakers,
            },
        )
