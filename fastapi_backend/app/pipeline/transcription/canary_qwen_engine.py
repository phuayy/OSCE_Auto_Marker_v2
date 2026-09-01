"""NVIDIA Canary-Qwen 2.5B engine.

Canary-Qwen is a speech-augmented language model: it returns punctuated,
capitalised English text and nothing else — no speaker labels, no subtitles,
and (in the SALM inference path) no word timestamps. It also has a 40-second
training horizon, so a 12-minute consultation must be decoded in chunks.

The engine therefore does three things around the model:

* runs ``scripts/canary_qwen_transcribe.py`` (NeMo lives in the scorer
  subprocess, exactly like the three scoring models, so a heavyweight optional
  dependency never loads inside the API process);
* runs a standalone pyannote pass and merges speaker turns onto the segments,
  because the scorers read speaker-tagged dialogue;
* renders the SRT/VTT the workspace player expects.

The result is written in the same raw shape WhisperX produces, so everything
downstream — normalisation, corpus correction, the LLM preprocess, scoring —
is unchanged.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from time import monotonic
from typing import Any

from app.core.config import Settings
from app.core.exceptions import EmptyTranscriptError, TranscriptionResourceError
from app.core.json_utils import extract_json_object
from app.core.process import CommandRunner
from app.pipeline.media import MediaPipeline
from app.pipeline.progress_tracker import ProgressTracker
from app.pipeline.transcription.base import (
    EngineAvailability,
    EngineCapabilities,
    EngineDescriptor,
    ParameterSpec,
    ParameterType,
    PrefetchResult,
    TranscriptionEngine,
    TranscriptionRequest,
    TranscriptionResult,
)
from app.pipeline.transcription.diarization import PyannoteDiarizer, assign_speakers
from app.pipeline.transcription.subtitles import build_srt_text, convert_srt_text_to_vtt_text
from app.services.event_service import EventService

ENGINE_ID = "canary-qwen"
TRANSCRIBE_SCHEMA = "canary-segments-v1"

# The script reports one counter over the chunk loop; the tail is the
# diarisation and subtitle work this engine does afterwards.
TRANSCRIBE_PROGRESS_SPAN = ((0.0, 80.0),)
DIARIZATION_PROGRESS = 90.0

# Printed by scripts/canary_qwen_transcribe.py when the checkpoint would not
# fit on any device. The raw signatures below are the safety net for an
# exhaustion that kills the interpreter before it reaches that handler — a
# native allocator abort, or a NeMo import that dies loading its own weights.
INSUFFICIENT_MEMORY_TOKEN = "canary-insufficient-memory:"
_MEMORY_ERROR_SIGNATURES = (
    INSUFFICIENT_MEMORY_TOKEN,
    "paging file is too small",
    "os error 1455",
    "winerror 1455",
    "cannot allocate memory",
    "not enough memory",
    "out of memory",
    "bad_alloc",
)

# How long an availability answer is reused. Long enough that polling the
# settings screen does not start an interpreter per request, short enough that
# installing NeMo is noticed without restarting the API.
AVAILABILITY_CACHE_SECONDS = 60.0

DESCRIPTOR = EngineDescriptor(
    id=ENGINE_ID,
    label="Canary-Qwen 2.5B",
    vendor="NVIDIA",
    description=(
        "NVIDIA's speech-augmented language model — the most accurate open English ASR on "
        "the Open ASR leaderboard. It produces text only, so this pipeline adds a pyannote "
        "diarisation pass to label the student and the simulated patient."
    ),
    capabilities=EngineCapabilities(
        diarization=False,
        word_timestamps=False,
        subtitles=False,
        progress=True,
    ),
    parameters=(
        ParameterSpec(
            name="model",
            label="Model",
            type=ParameterType.STRING,
            default="nvidia/canary-qwen-2.5b",
            help="HuggingFace id of the NeMo SALM checkpoint.",
        ),
        ParameterSpec(
            name="chunkSeconds",
            label="Chunk length (seconds)",
            type=ParameterType.FLOAT,
            default=30.0,
            minimum=5.0,
            maximum=40.0,
            help="Audio decoded per pass. The model was trained on segments up to 40s, and accuracy degrades beyond that.",
        ),
        ParameterSpec(
            name="overlapSeconds",
            label="Chunk overlap (seconds)",
            type=ParameterType.FLOAT,
            default=2.0,
            minimum=0.0,
            maximum=10.0,
            help="Audio shared between neighbouring chunks so a word spoken across a boundary is not lost.",
            advanced=True,
        ),
        ParameterSpec(
            name="batchSize",
            label="Batch size",
            type=ParameterType.INTEGER,
            default=1,
            minimum=1,
            maximum=32,
            help="Chunks decoded together. 1 is the safe setting on a 4-6 GB card.",
        ),
        ParameterSpec(
            name="device",
            label="Device",
            type=ParameterType.ENUM,
            default="auto",
            options=("auto", "cuda", "cpu"),
            help="auto uses CUDA when it is available and falls back to CPU.",
        ),
        ParameterSpec(
            name="diarize",
            label="Label speakers",
            type=ParameterType.BOOLEAN,
            default=True,
            help="Runs pyannote separately and assigns each segment the speaker it overlaps most. Turning this off leaves every line unattributed, which the scorers cannot interpret.",
        ),
        ParameterSpec(
            name="prompt",
            label="Transcription prompt",
            type=ParameterType.STRING,
            default="Transcribe the following:",
            help="Instruction given to the model before the audio. Rarely needs changing.",
            advanced=True,
        ),
    ),
    requirements=(
        "Needs the optional NeMo toolkit in the backend environment: "
        "pip install -r requirements-canary.txt (or pip install \"nemo_toolkit[asr]>=2.5\"). "
        "The ~5 GB checkpoint is downloaded into the HuggingFace cache at backend startup "
        "when this engine is selected, and on first run otherwise."
    ),
)


def is_memory_exhaustion(text: str) -> bool:
    """Whether a failed subprocess died of memory rather than of anything else.

    The subprocess normally classifies itself and prints one token; this reads
    the whole captured output so an unclassified crash — the allocator aborting
    inside a C++ extension, a traceback the script never got to catch — is still
    recognised for what it is.
    """
    lowered = str(text or "").lower()
    return any(signature in lowered for signature in _MEMORY_ERROR_SIGNATURES)


def memory_failure_message(model: str, text: str) -> str:
    """One operator-facing sentence, plus the line that actually diagnosed it.

    The raw failure is a hundred lines of NeMo, huggingface_hub and safetensors
    frames ending in one meaningful ``OSError``. That line is kept; the frames
    are not, because they name nothing the operator can change.
    """
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    cause = next(
        (
            line.split(INSUFFICIENT_MEMORY_TOKEN, 1)[-1].strip() if INSUFFICIENT_MEMORY_TOKEN in line else line
            for line in reversed(lines)
            if is_memory_exhaustion(line)
        ),
        "",
    )
    detail = f" ({cause})" if cause else ""
    return (
        f"This machine does not have enough memory to load {model}{detail}. "
        "The 2.5B-parameter checkpoint needs roughly 6 GB of free RAM or VRAM to read; on Windows "
        "the pagefile must be large enough to back it (a fixed or small pagefile fails with "
        "'The paging file is too small for this operation to complete'). "
        "Raise the pagefile, free memory, or select the WhisperX engine in Settings, "
        "which loads a much smaller model."
    )


class CanaryQwenEngine(TranscriptionEngine):
    descriptor = DESCRIPTOR

    def __init__(
        self,
        settings: Settings,
        runner: CommandRunner,
        events: EventService,
        media: MediaPipeline,
        diarizer: PyannoteDiarizer,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.events = events
        self.media = media
        self.diarizer = diarizer
        self._availability_cache: EngineAvailability | None = None
        self._availability_expires_at = 0.0

    def default_options(self) -> dict[str, Any]:
        settings = self.settings
        return {
            "model": settings.canary_model,
            "chunkSeconds": settings.canary_chunk_seconds,
            "overlapSeconds": settings.canary_overlap_seconds,
            "batchSize": settings.canary_batch_size,
            "device": settings.canary_device,
            "diarize": settings.canary_diarize,
            "prompt": settings.canary_prompt,
        }

    async def availability(self) -> EngineAvailability:
        """Report installability without importing NeMo into this process.

        The script's presence is checked directly; the toolkit is checked in
        the scorer interpreter, which is where it would have to be installed.
        The answer is cached briefly because the settings screen asks on every
        load and each check costs an interpreter start — but not for the
        session's lifetime, so installing NeMo takes effect without a restart.
        """
        now = monotonic()
        if self._availability_cache is not None and now < self._availability_expires_at:
            return self._availability_cache
        availability = await self._probe_availability()
        self._availability_cache = availability
        self._availability_expires_at = now + AVAILABILITY_CACHE_SECONDS
        return availability

    async def _probe_availability(self) -> EngineAvailability:
        script_path = self.settings.canary_script_path
        if not script_path.exists():
            return EngineAvailability(False, f"Transcription script missing at {script_path}.")
        try:
            result = await self.runner.run(
                self.settings.scorer_python_bin,
                [str(script_path), "--check"],
                "Canary-Qwen availability check",
                env=self.settings.subprocess_env(),
            )
        except Exception as error:  # a missing interpreter is an answer, not a crash
            return EngineAvailability(False, f"Could not run the availability check: {error}")
        if "nemo-ready" in str(result.stdout or ""):
            return EngineAvailability(True)
        return EngineAvailability(False, "The NeMo toolkit is not installed in the backend environment.")

    async def prefetch(self) -> PrefetchResult:
        """Cache the checkpoint before anyone asks for a transcript.

        Runs in the scorer interpreter, like every other Canary call, and only
        downloads — the model is never constructed here, so startup claims no
        GPU memory. Already-cached files make this a fast no-op, which is why
        it can run on every boot.
        """
        availability = await self.availability()
        if not availability.available:
            return PrefetchResult(False, availability.reason)
        model = self.settings.canary_model
        try:
            result = await self.runner.run(
                self.settings.scorer_python_bin,
                [str(self.settings.canary_script_path), "--download", "--model", model],
                "Canary-Qwen weight download",
                env=self.diarizer.python_env(),
                # A cold ~5 GB download on a slow link outlasts the default
                # watchdog, and killing it halfway wastes everything fetched.
                timeout_seconds=None,
            )
        except Exception as error:
            return PrefetchResult(False, f"Could not download {model}: {error}")
        if "model-ready" in str(result.stdout or ""):
            return PrefetchResult(True, f"{model} is cached and ready.")
        return PrefetchResult(False, f"{model} was not cached; it will download on the first run.")

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        options = request.options
        request.output_dir.mkdir(parents=True, exist_ok=True)
        audio_path = await self.media.prepare_transcription_wav(
            {"fileName": request.audio_file_name, "absolutePath": str(request.audio_path)},
            self.settings.canary_audio_filters,
        )
        raw_output_path = request.output_dir / f"{request.output_base_name}.canary.json"

        segments = await self._run_model(request, audio_path, raw_output_path)
        if not segments:
            raise EmptyTranscriptError(
                "Canary-Qwen produced no speech segments. The recording may be silent, "
                "or its audio track failed to extract."
            )

        diarized = False
        if options.get("diarize", True):
            diarized = await self._apply_diarization(request, audio_path, segments)

        json_path, srt_path, vtt_path = await asyncio.to_thread(
            self._write_artifacts, request, segments, options
        )
        return TranscriptionResult(
            json_path=json_path,
            srt_path=srt_path,
            vtt_path=vtt_path,
            engine_id=ENGINE_ID,
            model=str(options.get("model") or self.settings.canary_model),
            diarized=diarized,
            metadata={
                "chunkSeconds": options.get("chunkSeconds"),
                "batchSize": options.get("batchSize"),
                "segmentCount": len(segments),
                "diarized": diarized,
            },
        )

    async def _run_model(
        self,
        request: TranscriptionRequest,
        audio_path: Path,
        output_path: Path,
    ) -> list[dict[str, Any]]:
        options = request.options
        args = [
            str(self.settings.canary_script_path),
            "--audio",
            str(audio_path),
            "--output",
            str(output_path),
            "--model",
            str(options.get("model") or self.settings.canary_model),
            "--chunk-seconds",
            str(options.get("chunkSeconds")),
            "--overlap-seconds",
            str(options.get("overlapSeconds")),
            "--batch-size",
            str(options.get("batchSize")),
            "--device",
            str(options.get("device") or "auto"),
            "--language",
            request.language or "en",
            "--prompt",
            str(options.get("prompt") or self.settings.canary_prompt),
        ]
        await self.events.publish(
            request.session_id,
            "log",
            {"source": ENGINE_ID, "message": f"{self.settings.scorer_python_bin} {' '.join(args)}"},
        )
        model = str(options.get("model") or self.settings.canary_model)
        try:
            await self.runner.run(
                self.settings.scorer_python_bin,
                args,
                "Canary-Qwen transcription",
                env=self.settings.subprocess_env(),
                on_output=self._build_output_handler(request),
            )
        except Exception as error:
            # A host that cannot hold the checkpoint fails the same way on every
            # attempt, so it is raised as a typed, non-retryable error the queue
            # will not spend the job's remaining attempts on. Everything else
            # keeps its original exception and its retry.
            if is_memory_exhaustion(str(error)):
                raise TranscriptionResourceError(memory_failure_message(model, str(error))) from error
            raise
        if not output_path.exists():
            raise RuntimeError("Canary-Qwen transcription produced no output file.")
        payload = await asyncio.to_thread(
            lambda: extract_json_object(output_path.read_text(encoding="utf-8"))
        )
        if str(payload.get("schema") or "") != TRANSCRIBE_SCHEMA:
            raise RuntimeError("Canary-Qwen output had an unexpected schema.")
        raw_segments = payload.get("segments")
        return [segment for segment in raw_segments if isinstance(segment, dict)] if isinstance(raw_segments, list) else []

    def _build_output_handler(self, request: TranscriptionRequest):
        tracker = ProgressTracker(phase_spans=TRANSCRIBE_PROGRESS_SPAN)

        async def handle(stream: str, text: str) -> None:
            await self.events.publish(
                request.session_id,
                "log",
                {"source": f"{ENGINE_ID}-{stream}", "message": text},
            )
            percent = tracker.update(text)
            if percent is None:
                return
            await self.events.publish(
                request.session_id,
                "progress",
                {"step": "transcription", "percent": percent},
            )
            if request.on_progress is not None:
                await request.on_progress(percent)

        return handle

    async def _apply_diarization(
        self,
        request: TranscriptionRequest,
        audio_path: Path,
        segments: list[dict[str, Any]],
    ) -> bool:
        """Label the segments, or leave them unattributed and say so.

        Best-effort by design: a diarisation failure must not throw away a
        good transcription. The transcript proceeds with SPEAKER_UNKNOWN, and
        the run is marked as undiarised so the operator can see why the
        scorers had no speaker structure to read.
        """
        turns_path = request.output_dir / f"{request.output_base_name}.speakers.json"
        try:
            turns = await self.diarizer.run(
                request.session_id,
                audio_path,
                turns_path,
                min_speakers=request.min_speakers,
                max_speakers=request.max_speakers,
            )
            labelled = assign_speakers(segments, turns)
        except Exception as error:
            await self.events.publish(
                request.session_id,
                "log",
                {
                    "source": "diarization",
                    "message": f"Speaker diarization failed ({error}); the transcript keeps unlabelled speakers.",
                },
            )
            return False

        if request.on_progress is not None:
            await request.on_progress(DIARIZATION_PROGRESS)
        await self.events.publish(
            request.session_id,
            "log",
            {
                "source": "diarization",
                "message": f"Assigned speakers to {labelled}/{len(segments)} segment(s) from {len(turns)} turn(s).",
            },
        )
        return labelled > 0

    def _write_artifacts(
        self,
        request: TranscriptionRequest,
        segments: list[dict[str, Any]],
        options: dict[str, Any],
    ) -> tuple[Path, Path, Path]:
        """Persist the raw transcript plus the subtitles, WhisperX-style.

        The JSON deliberately matches WhisperX's raw output shape — the
        pipeline's normaliser, empty-transcript guard and artifact cache all
        read it — with an added ``engine`` marker so one engine never resumes
        from another's artifact.
        """
        json_path = request.output_dir / f"{request.output_base_name}.json"
        payload = {
            "engine": ENGINE_ID,
            "model": str(options.get("model") or self.settings.canary_model),
            "language": request.language or "en",
            "segments": segments,
        }
        json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        srt_path = request.output_dir / f"{request.output_base_name}.srt"
        srt_text = build_srt_text(segments)
        srt_path.write_text(srt_text, encoding="utf-8")
        vtt_path = request.output_dir / f"{request.output_base_name}.vtt"
        vtt_path.write_text(convert_srt_text_to_vtt_text(srt_text), encoding="utf-8")
        return json_path, srt_path, vtt_path
