"""A full run on each engine, with only the subprocess boundary faked.

The unit suites pin each part on its own. This one proves the parts compose:
the operator's stored selection reaches the router, the router's engine
produces artifacts the pipeline can normalise, and the session records which
engine did the work — for WhisperX, which diarises itself, and for Canary-Qwen,
which needs the pipeline to add speaker labels and subtitles.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.core.config import Settings
from app.core.resources import ResourceLease
from app.core.process import CommandResult
from app.pipeline.media import MediaPipeline
from app.pipeline.transcription import registry
from app.pipeline.transcription.diarization import DIARIZATION_SCHEMA
from app.services.pipeline_service import TRANSCRIPTION_STEP, PipelineService
from app.services.transcription_router import TranscriptionRouter

from tests.test_pipeline_service import FakeEvents, FakeSessions
from tests.test_transcription_diarization import stub_script_root

WHISPERX_JSON = {
    "language": "en",
    "segments": [
        {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00", "text": "Good morning."},
        {"start": 2.0, "end": 4.0, "speaker": "SPEAKER_01", "text": "Morning."},
    ],
}
CANARY_SEGMENTS = [
    {"start": 0.0, "end": 3.0, "text": "Good morning, I am the medical student."},
    {"start": 3.0, "end": 6.0, "text": "Morning, my throat hurts."},
]
TURNS = [
    {"start": 0.0, "end": 3.0, "speaker": "SPEAKER_00"},
    {"start": 3.0, "end": 6.0, "speaker": "SPEAKER_01"},
]


class ScriptedRunner:
    """Fakes ffmpeg, the WhisperX CLI, the Canary script and the diariser."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.commands: list[tuple[str, list[str]]] = []

    async def run(self, command: str, args: list[str], _label: str, **kwargs: Any) -> CommandResult:
        self.commands.append((command, list(args)))
        if command == self.settings.ffmpeg_bin:
            Path(args[-1]).write_bytes(b"fake-audio")
            return CommandResult(stdout="", stderr="")

        if command == self.settings.whisperx_bin:
            await self._stream(kwargs.get("on_output"), ["Progress: 100.00%..."])
            output_dir = Path(args[args.index("--output_dir") + 1])
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / f"{Path(args[0]).stem}.json").write_text(json.dumps(WHISPERX_JSON), encoding="utf-8")
            return CommandResult(stdout="", stderr="")

        script = Path(args[0]).name
        if "--check" in args:
            # The router asks the engine whether it can run before running it;
            # this environment has NeMo, as far as the pipeline is concerned.
            return CommandResult(stdout="nemo-ready", stderr="")
        output_path = Path(args[args.index("--output") + 1])
        if script == "pyannote_diarize.py":
            output_path.write_text(json.dumps({"schema": DIARIZATION_SCHEMA, "turns": TURNS}), encoding="utf-8")
            return CommandResult(stdout="", stderr="")

        await self._stream(kwargs.get("on_output"), ["Progress: 50.00%...", "Progress: 100.00%..."])
        output_path.write_text(
            json.dumps({"schema": "canary-segments-v1", "segments": CANARY_SEGMENTS}), encoding="utf-8"
        )
        return CommandResult(stdout="", stderr="")

    @staticmethod
    async def _stream(on_output, lines: list[str]) -> None:
        for line in lines:
            if on_output is not None:
                result = on_output("stdout", line)
                if asyncio.iscoroutine(result):
                    await result

    def ran(self, name: str) -> bool:
        return any(Path(args[0]).name == name for _, args in self.commands if args)


class StubAppSettings:
    def __init__(self, engine_id: str, options: dict[str, Any] | None = None) -> None:
        self.engine_id = engine_id
        self.options = options or {}

    async def transcription_selection(self, user_id: str | None = None) -> tuple[str, dict[str, Any]]:
        return self.engine_id, self.options

    async def llm_preprocess_enabled(self, user_id: str | None = None) -> bool:
        return False


def run_pipeline(tmp_path: Path, engine_id: str) -> tuple[FakeSessions, ScriptedRunner]:
    settings = Settings(
        root_dir=stub_script_root(tmp_path),
        backend_root=tmp_path,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        whisperx_device="cpu",
        enable_scoring=False,
        enable_audio_professionalism=False,
        enable_communication_scoring=False,
    )
    settings.paths.ensure_layout()

    runner = ScriptedRunner(settings)
    events = FakeEvents()
    auth = SimpleNamespace(runtime=SimpleNamespace(whisperx_hf_token="hf-token"))
    media = MediaPipeline(settings, runner, events, auth, gpu=ResourceLease.unbounded())
    preferences = StubAppSettings(engine_id)
    router = TranscriptionRouter(
        settings,
        events,
        registry.EngineDependencies(settings, runner, events, auth, media),
        preferences=preferences,
    )

    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"fake-video")
    sessions = FakeSessions(
        {
            "id": "session-1",
            "name": "Station 1",
            "status": "uploaded",
            "workflow": "standard",
            "files": {"video": {"absolutePath": str(video_path)}, "caseStudy": None},
            "outputs": {},
            "pipeline": {},
        }
    )
    service = PipelineService(
        sessions=sessions,
        events=events,
        media=media,
        scoring=object(),
        preferences=preferences,
        transcription=router,
    )
    asyncio.run(service.process_session_by_id("session-1"))
    return sessions, runner


def final_session(sessions: FakeSessions) -> dict[str, Any]:
    return sessions.writes[-1]


def transcript_of(sessions: FakeSessions) -> dict[str, Any]:
    path = Path(final_session(sessions)["outputs"]["transcript"]["absolutePath"])
    return json.loads(path.read_text(encoding="utf-8"))


def test_whisperx_selection_runs_the_cli_and_no_extra_diarization(tmp_path: Path) -> None:
    sessions, runner = run_pipeline(tmp_path, "whisperx")

    assert any(command.endswith("whisperx") for command, _ in runner.commands)
    assert not runner.ran("pyannote_diarize.py")
    assert final_session(sessions)["transcription"]["engineId"] == "whisperx"


def test_canary_selection_runs_the_script_and_the_diarization_pass(tmp_path: Path) -> None:
    sessions, runner = run_pipeline(tmp_path, "canary-qwen")

    assert runner.ran("canary_qwen_transcribe.py")
    assert runner.ran("pyannote_diarize.py")
    assert not any(command.endswith("whisperx") for command, _ in runner.commands)
    assert final_session(sessions)["transcription"]["engineId"] == "canary-qwen"


def test_both_engines_yield_the_same_transcript_schema(tmp_path: Path) -> None:
    # Everything downstream — corpus correction, the LLM preprocess, all three
    # scorers — reads this document, so the engines must be interchangeable
    # from the normaliser onwards.
    whisperx_sessions, _ = run_pipeline(tmp_path / "a", "whisperx")
    canary_sessions, _ = run_pipeline(tmp_path / "b", "canary-qwen")

    for sessions in (whisperx_sessions, canary_sessions):
        transcript = transcript_of(sessions)
        assert transcript["schema"] == "whisperx-segments-v1"
        assert transcript["segmentCount"] == 2
        assert [segment["speaker"] for segment in transcript["segments"]] == ["SPEAKER_00", "SPEAKER_01"]


def test_canary_runs_produce_the_subtitle_track_the_player_needs(tmp_path: Path) -> None:
    sessions, _ = run_pipeline(tmp_path, "canary-qwen")

    outputs = final_session(sessions)["outputs"]
    assert outputs["subtitle"]["fileName"].endswith(".srt")
    assert outputs["subtitleTrack"]["fileName"].endswith(".vtt")
    assert Path(outputs["subtitleTrack"]["absolutePath"]).read_text(encoding="utf-8").startswith("WEBVTT")


def test_the_step_records_which_engine_and_model_ran(tmp_path: Path) -> None:
    sessions, _ = run_pipeline(tmp_path, "canary-qwen")

    step = final_session(sessions)["pipeline"]["steps"][TRANSCRIPTION_STEP]
    assert step["status"] == "completed"
    assert step["metadata"]["engineLabel"] == "Canary-Qwen 2.5B"
    assert step["metadata"]["model"] == "nvidia/canary-qwen-2.5b"
    assert step["metadata"]["diarized"] is True


def test_live_progress_is_recorded_whichever_engine_runs(tmp_path: Path) -> None:
    for engine_id in ("whisperx", "canary-qwen"):
        sessions, _ = run_pipeline(tmp_path / engine_id, engine_id)

        readings = [
            write["pipeline"]["stepProgress"]
            for write in sessions.writes
            if write.get("pipeline", {}).get("currentStep") == TRANSCRIPTION_STEP
            and write["pipeline"].get("stepProgress") is not None
        ]
        assert readings, f"{engine_id} reported no progress"
        assert readings == sorted(readings)
        assert final_session(sessions)["pipeline"]["stepProgress"] is None
