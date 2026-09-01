"""End-to-end check that the three WhisperX changes work together.

The unit suites pin each piece on its own (flag assembly, progress parsing,
step-progress persistence, the projection field). This one runs a full fresh
transcription through the real ``PipelineService`` and the real
``MediaPipeline`` with only the subprocess boundary faked, so a regression in
the wiring *between* them — the callback the pipeline hands to media, the lock,
the step lifecycle — fails here even when every unit test still passes.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.core.config import Settings
from app.core.process import CommandResult
from app.pipeline.media import MediaPipeline
from app.services.pipeline_service import TRANSCRIPTION_STEP, PipelineService

from tests.test_pipeline_service import FakeEvents, FakeSessions

TRANSCRIBED = {
    "language": "en",
    "segments": [
        {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00", "text": "Good morning, I am the medical student."},
        {"start": 2.0, "end": 4.0, "speaker": "SPEAKER_01", "text": "Morning doctor."},
    ],
}

# Transcription then alignment, each counting from zero — what the real CLI
# prints under --print_progress.
WHISPERX_STDOUT = [
    "Performing transcription...",
    "Progress: 33.33%...",
    "Progress: 66.67%...",
    "Progress: 100.00%...",
    "Performing alignment...",
    "Progress: 50.00%...",
    "Progress: 100.00%...",
]


class StreamingRunner:
    """The WhisperX/ffmpeg subprocess boundary: streams stdout, writes files."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.whisperx_args: list[str] = []

    async def run(self, command: str, args: list[str], _label: str, **kwargs: Any) -> CommandResult:
        if command == self.settings.ffmpeg_bin:
            Path(args[-1]).write_bytes(b"fake-audio")
            return CommandResult(stdout="", stderr="")

        self.whisperx_args = list(args)
        on_output = kwargs.get("on_output")
        for line in WHISPERX_STDOUT:
            if on_output is not None:
                result = on_output("stdout", line)
                if asyncio.iscoroutine(result):
                    await result
        output_dir = Path(args[args.index("--output_dir") + 1])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / f"{Path(args[0]).stem}.json").write_text(json.dumps(TRANSCRIBED), encoding="utf-8")
        return CommandResult(stdout="", stderr="")


def build(tmp_path: Path) -> tuple[PipelineService, FakeSessions, StreamingRunner]:
    settings = Settings(
        root_dir=tmp_path,
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
    runner = StreamingRunner(settings)
    auth = SimpleNamespace(runtime=SimpleNamespace(whisperx_hf_token="hf-token"))
    media = MediaPipeline(settings, runner, FakeEvents(), auth)

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
    service = PipelineService(sessions=sessions, events=FakeEvents(), media=media, scoring=object())
    return service, sessions, runner


def run_pipeline(tmp_path: Path) -> tuple[FakeSessions, StreamingRunner]:
    service, sessions, runner = build(tmp_path)
    asyncio.run(service.process_session_by_id("session-1"))
    return sessions, runner


def test_a_real_run_carries_all_three_flags(tmp_path: Path) -> None:
    _, runner = run_pipeline(tmp_path)

    args = runner.whisperx_args
    assert args[args.index("--min_speakers") + 1] == "2"
    assert args[args.index("--max_speakers") + 1] == "2"
    assert args[args.index("--chunk_size") + 1] == "20"
    assert args[args.index("--print_progress") + 1] == "True"


def test_progress_is_persisted_while_transcription_runs(tmp_path: Path) -> None:
    sessions, _ = run_pipeline(tmp_path)

    readings = [
        write["pipeline"]["stepProgress"]
        for write in sessions.writes
        if write.get("pipeline", {}).get("currentStep") == TRANSCRIPTION_STEP
        and write["pipeline"].get("stepProgress") is not None
    ]
    assert readings, "no live progress was written during the WhisperX step"
    assert readings == sorted(readings)
    assert readings[-1] <= 100


def test_progress_is_cleared_once_the_run_completes(tmp_path: Path) -> None:
    sessions, _ = run_pipeline(tmp_path)

    final = sessions.writes[-1]
    assert final["status"] == "completed"
    assert final["pipeline"]["stepProgress"] is None
    assert final["pipeline"]["currentStep"] is None
    assert final["pipeline"]["steps"][TRANSCRIPTION_STEP]["status"] == "completed"


def test_the_transcript_is_unaffected_by_the_progress_plumbing(tmp_path: Path) -> None:
    sessions, _ = run_pipeline(tmp_path)

    transcript_path = Path(sessions.writes[-1]["outputs"]["transcript"]["absolutePath"])
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    assert transcript["schema"] == "whisperx-segments-v1"
    assert transcript["segmentCount"] == 2
    assert [segment["speaker"] for segment in transcript["segments"]] == ["SPEAKER_00", "SPEAKER_01"]
