"""The Canary-Qwen engine.

Canary returns punctuated text and nothing else, so this engine has to supply
everything else the pipeline expects: chunked decoding through a subprocess,
speaker labels from a separate diarisation pass, subtitles rendered from the
segments, and a raw JSON document in the shape the normaliser reads.

The NeMo model is never loaded here — the subprocess boundary is faked, exactly
as the scorer suites fake theirs.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings
from app.core.exceptions import EmptyTranscriptError
from app.core.process import CommandResult
from app.pipeline.media import MediaPipeline
from app.pipeline.transcription.canary_qwen_engine import CanaryQwenEngine
from app.pipeline.transcription.base import TranscriptionRequest
from app.pipeline.transcription.diarization import DIARIZATION_SCHEMA, PyannoteDiarizer

from tests.test_transcription_diarization import stub_script_root

CANARY_SEGMENTS = [
    {"start": 0.0, "end": 30.0, "text": "Good morning, I am the medical student."},
    {"start": 28.0, "end": 55.0, "text": "Morning doctor, my throat hurts."},
]
TURNS = [
    {"start": 0.0, "end": 27.0, "speaker": "SPEAKER_00"},
    {"start": 27.0, "end": 60.0, "speaker": "SPEAKER_01"},
]
CANARY_STDOUT = ["Loading nvidia/canary-qwen-2.5b...", "Progress: 50.00%...", "Progress: 100.00%..."]


class FakeEvents:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict[str, Any]]] = []

    async def publish(self, session_id: str, event: str, payload: dict[str, Any]) -> None:
        self.items.append((session_id, event, payload))


class FakeRunner:
    """Fakes ffmpeg, the Canary script and the diarisation script."""

    def __init__(
        self,
        settings: Settings,
        segments: list[dict[str, Any]] | None,
        turns: list[dict[str, Any]] | None,
        stdout: list[str],
        diarization_error: bool = False,
    ) -> None:
        self.settings = settings
        self.segments = segments
        self.turns = turns
        self.stdout = stdout
        self.diarization_error = diarization_error
        self.calls: list[tuple[str, list[str]]] = []

    async def run(self, command: str, args: list[str], _label: str, **kwargs: Any) -> CommandResult:
        self.calls.append((command, list(args)))
        if command == self.settings.ffmpeg_bin:
            Path(args[-1]).write_bytes(b"fake-wav")
            return CommandResult(stdout="", stderr="")

        script = Path(args[0]).name
        if script == "pyannote_diarize.py":
            if self.diarization_error:
                raise RuntimeError("pyannote exploded")
            if self.turns is not None:
                Path(args[args.index("--output") + 1]).write_text(
                    json.dumps({"schema": DIARIZATION_SCHEMA, "turns": self.turns}), encoding="utf-8"
                )
            return CommandResult(stdout="", stderr="")

        on_output = kwargs.get("on_output")
        for line in self.stdout:
            if on_output is not None:
                result = on_output("stdout", line)
                if asyncio.iscoroutine(result):
                    await result
        if self.segments is not None:
            Path(args[args.index("--output") + 1]).write_text(
                json.dumps({"schema": "canary-segments-v1", "segments": self.segments}), encoding="utf-8"
            )
        return CommandResult(stdout="", stderr="")

    def script_args(self, script_name: str) -> list[str]:
        return next(args for command, args in self.calls if Path(args[0]).name == script_name)


def build(
    tmp_path: Path,
    *,
    segments: list[dict[str, Any]] | None = None,
    turns: list[dict[str, Any]] | None = None,
    stdout: list[str] | None = None,
    diarization_error: bool = False,
    write_output: bool = True,
    **setting_overrides: Any,
) -> tuple[CanaryQwenEngine, FakeRunner, FakeEvents]:
    settings = Settings(
        root_dir=stub_script_root(tmp_path),
        backend_root=tmp_path,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        **setting_overrides,
    )
    runner = FakeRunner(
        settings,
        (CANARY_SEGMENTS if segments is None else segments) if write_output else None,
        TURNS if turns is None else turns,
        CANARY_STDOUT if stdout is None else stdout,
        diarization_error,
    )
    events = FakeEvents()
    auth = SimpleNamespace(runtime=SimpleNamespace(whisperx_hf_token="hf-token"))
    media = MediaPipeline(settings, runner, events, auth)
    diarizer = PyannoteDiarizer(settings, runner, events, auth)
    return CanaryQwenEngine(settings, runner, events, media, diarizer), runner, events


def transcribe(engine: CanaryQwenEngine, tmp_path: Path, **request_overrides: Any):
    audio_path = tmp_path / "session-1.mp3"
    audio_path.write_bytes(b"fake-mp3")
    output_dir = tmp_path / "artifacts"
    request = TranscriptionRequest(
        session_id="session-1",
        audio_path=audio_path,
        audio_file_name=audio_path.name,
        output_dir=output_dir,
        language="en",
        options=engine.resolve_options(request_overrides.pop("options", None)),
        **request_overrides,
    )
    return asyncio.run(engine.transcribe(request))


def test_the_engine_declares_what_it_cannot_do(tmp_path: Path) -> None:
    # These flags are what make the router add a diarisation pass and render
    # subtitles; getting them wrong silently degrades every Canary run.
    engine, _, _ = build(tmp_path)

    capabilities = engine.descriptor.capabilities
    assert capabilities.diarization is False
    assert capabilities.subtitles is False
    assert capabilities.progress is True


def test_script_arguments_carry_the_resolved_options(tmp_path: Path) -> None:
    engine, runner, _ = build(tmp_path)

    transcribe(engine, tmp_path, options={"chunkSeconds": 25.0, "batchSize": 2, "device": "cpu"})

    args = runner.script_args("canary_qwen_transcribe.py")
    assert args[args.index("--chunk-seconds") + 1] == "25.0"
    assert args[args.index("--batch-size") + 1] == "2"
    assert args[args.index("--device") + 1] == "cpu"
    assert args[args.index("--model") + 1] == "nvidia/canary-qwen-2.5b"


def test_audio_is_converted_before_the_model_sees_it(tmp_path: Path) -> None:
    # Canary requires 16 kHz mono; the extracted MP3 must stay untouched
    # because the audio-professionalism scorer measures loudness on it.
    engine, runner, _ = build(tmp_path)

    transcribe(engine, tmp_path)

    ffmpeg_args = next(args for command, args in runner.calls if command == "ffmpeg")
    assert ffmpeg_args[ffmpeg_args.index("-ar") + 1] == "16000"
    assert ffmpeg_args[ffmpeg_args.index("-ac") + 1] == "1"
    assert (tmp_path / "session-1.mp3").read_bytes() == b"fake-mp3"


def test_speakers_are_assigned_from_the_diarization_pass(tmp_path: Path) -> None:
    engine, _, _ = build(tmp_path)

    result = transcribe(engine, tmp_path)

    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert [segment["speaker"] for segment in payload["segments"]] == ["SPEAKER_00", "SPEAKER_01"]
    assert result.diarized is True


def test_the_station_speaker_bounds_reach_the_diarizer(tmp_path: Path) -> None:
    engine, runner, _ = build(tmp_path)

    transcribe(engine, tmp_path, min_speakers=2, max_speakers=2)

    args = runner.script_args("pyannote_diarize.py")
    assert args[args.index("--min-speakers") + 1] == "2"
    assert args[args.index("--max-speakers") + 1] == "2"


def test_diarization_can_be_turned_off(tmp_path: Path) -> None:
    engine, runner, _ = build(tmp_path)

    result = transcribe(engine, tmp_path, options={"diarize": False})

    assert result.diarized is False
    assert not any(Path(args[0]).name == "pyannote_diarize.py" for _, args in runner.calls)


def test_a_diarization_failure_keeps_the_transcript(tmp_path: Path) -> None:
    # Losing a good transcription because the speaker pass crashed would waste
    # the whole run; the transcript proceeds unlabelled and says so.
    engine, _, events = build(tmp_path, diarization_error=True)

    result = transcribe(engine, tmp_path)

    assert result.diarized is False
    assert result.json_path.exists()
    assert any("failed" in str(payload.get("message", "")) for _, _, payload in events.items)


def test_subtitles_are_rendered_for_the_player(tmp_path: Path) -> None:
    engine, _, _ = build(tmp_path)

    result = transcribe(engine, tmp_path)

    assert result.srt_path.exists() and result.vtt_path.exists()
    assert result.vtt_path.read_text(encoding="utf-8").startswith("WEBVTT")
    assert "[SPEAKER_00]" in result.srt_path.read_text(encoding="utf-8")


def test_the_raw_json_is_readable_by_the_pipeline_normalizer(tmp_path: Path) -> None:
    engine, runner, _ = build(tmp_path)
    media = MediaPipeline(runner.settings, runner, FakeEvents(), SimpleNamespace(runtime=None))

    result = transcribe(engine, tmp_path)
    normalized = media.normalize_whisperx_transcript(
        json.loads(result.json_path.read_text(encoding="utf-8"))
    )

    assert normalized["schema"] == "whisperx-segments-v1"
    assert normalized["segmentCount"] == 2
    assert normalized["segments"][0]["speaker"] == "SPEAKER_00"


def test_the_artifact_is_marked_so_whisperx_never_resumes_from_it(tmp_path: Path) -> None:
    engine, runner, _ = build(tmp_path)

    result = transcribe(engine, tmp_path)
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    media = MediaPipeline(runner.settings, runner, FakeEvents(), SimpleNamespace(runtime=None))

    assert payload["engine"] == "canary-qwen"
    # The WhisperX cache probe must reject another engine's artifact.
    cached = asyncio.run(
        media.find_existing_whisperx_outputs(
            {"id": "session-1"}, {"fileName": "session-1.mp3", "absolutePath": ""}
        )
    )
    assert cached is None


def test_progress_lines_are_forwarded_and_capped_below_completion(tmp_path: Path) -> None:
    engine, _, events = build(tmp_path)
    reported: list[float] = []

    async def on_progress(percent: float) -> None:
        reported.append(percent)

    transcribe(engine, tmp_path, on_progress=on_progress)

    assert reported == sorted(reported)
    # The chunk loop tops out at 80; diarisation reports the last stretch.
    assert max(reported) <= 90.0
    assert any(event == "progress" for _, event, _ in events.items)


def test_a_silent_recording_fails_the_run_rather_than_scoring_nothing(tmp_path: Path) -> None:
    engine, _, _ = build(tmp_path, segments=[])

    with pytest.raises(EmptyTranscriptError, match="no speech segments"):
        transcribe(engine, tmp_path)


def test_a_missing_output_file_is_reported(tmp_path: Path) -> None:
    engine, _, _ = build(tmp_path, write_output=False)

    with pytest.raises(RuntimeError, match="no output file"):
        transcribe(engine, tmp_path)


def test_availability_reports_a_missing_nemo_install(tmp_path: Path) -> None:
    engine, _, _ = build(tmp_path)

    class ProbeRunner:
        async def run(self, _command: str, _args: list[str], _label: str, **_kwargs: Any) -> CommandResult:
            return CommandResult(stdout="nemo-missing\n", stderr="")

    engine.runner = ProbeRunner()
    availability = asyncio.run(engine.availability())

    assert availability.available is False
    assert "NeMo" in availability.reason


def test_availability_confirms_an_installed_toolkit(tmp_path: Path) -> None:
    engine, _, _ = build(tmp_path)

    class ProbeRunner:
        async def run(self, _command: str, _args: list[str], _label: str, **_kwargs: Any) -> CommandResult:
            return CommandResult(stdout="nemo-ready\n", stderr="")

    engine.runner = ProbeRunner()

    assert asyncio.run(engine.availability()).available is True


def test_the_availability_probe_is_cached_between_settings_loads(tmp_path: Path) -> None:
    # The settings screen asks on every load; each uncached answer costs an
    # interpreter start.
    engine, _, _ = build(tmp_path)

    class CountingRunner:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, _command: str, _args: list[str], _label: str, **_kwargs: Any) -> CommandResult:
            self.calls += 1
            return CommandResult(stdout="nemo-ready", stderr="")

    probe = CountingRunner()
    engine.runner = probe

    assert asyncio.run(engine.availability()).available is True
    assert asyncio.run(engine.availability()).available is True
    assert probe.calls == 1


def test_the_cached_answer_expires(tmp_path: Path) -> None:
    # Installing NeMo must take effect without restarting the API.
    engine, _, _ = build(tmp_path)

    class ReadyRunner:
        async def run(self, _command: str, _args: list[str], _label: str, **_kwargs: Any) -> CommandResult:
            return CommandResult(stdout="nemo-ready", stderr="")

    engine.runner = ReadyRunner()
    asyncio.run(engine.availability())
    engine._availability_expires_at = 0.0

    assert asyncio.run(engine.availability()).available is True
