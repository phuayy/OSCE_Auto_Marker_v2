"""Live progress from the human detector's stderr through to the callback.

The detector is the longest thing a long-workflow session does and, before
this, the quietest: it wrote nothing between "started" and "finished", so the
session card sat at a fixed midpoint and the change stream had nothing to
announce for the length of the run. It now prints the pipeline's standard
``Progress: N%`` token, and this covers the seam that lifts it out — without
disturbing the log stream the workspace's live panel renders.
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

from tests.test_whisperx_invocation import CapturingEvents

# What scripts/detect_human_segments.py actually writes, interleaved with the
# lines that carry no reading at all.
DETECTOR_STDERR = [
    "[human-segments] Loading detector PekingU/rtdetr_v2_r18vd on cuda (fp16=True)",
    "[human-segments] Analysed 120 frames — Progress: 10.0%",
    "[human-segments] Analysed 600 frames — Progress: 50.0%",
    "[human-segments] Analysed 1200 frames — Progress: 100.0%",
    "[human-segments] done",
]

DETECTOR_PAYLOAD = {
    "clip_ranges": [{"start": 0.0, "end": 60.0}, {"start": 70.0, "end": 120.0}],
    "timeline_segments": [
        {"start": 0.0, "end": 60.0, "kind": "session", "person_count": 2},
        {"start": 60.0, "end": 70.0, "kind": "intermission", "person_count": 1},
        {"start": 70.0, "end": 120.0, "kind": "session", "person_count": 2},
    ],
    "detector": "osce-human-presence-rtdetr-v2",
    "debug": {"workers": 1},
}


class DetectorRunner:
    """Replays scripted stderr through the caller's ``on_output`` hook, the way
    CommandRunner streams a subprocess, then returns the JSON document."""

    def __init__(self, stderr_lines: list[str]) -> None:
        self.stderr_lines = list(stderr_lines)

    async def run(self, command: str, args: list[str], label: str, **kwargs: Any) -> CommandResult:
        on_output = kwargs.get("on_output")
        for line in self.stderr_lines:
            if on_output is None:
                continue
            result = on_output("stderr", line)
            if asyncio.iscoroutine(result):
                await result
        return CommandResult(stdout=json.dumps(DETECTOR_PAYLOAD), stderr="")


def make_media(tmp_path: Path, stderr_lines: list[str]) -> MediaPipeline:
    settings = Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        app_database_url="",
        database_url="",
    )
    script = settings.human_detector_script_path
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# stand-in for the detector script", encoding="utf-8")
    auth = SimpleNamespace(runtime=SimpleNamespace(whisperx_hf_token="hf-token"))
    return MediaPipeline(settings, runner=DetectorRunner(stderr_lines), events=CapturingEvents(), auth=auth)


def run_detection(media: MediaPipeline, tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
    video = tmp_path / "long.mp4"
    video.write_bytes(b"video")
    return asyncio.run(
        media.detect_person_clip_ranges_with_python(video, 120.0, session_id="session-1", **kwargs)
    )


def collect_progress(media: MediaPipeline, tmp_path: Path) -> list[float]:
    reported: list[float] = []

    async def on_progress(percent: float) -> None:
        reported.append(percent)

    run_detection(media, tmp_path, on_progress=on_progress)
    return reported


def test_progress_lines_reach_the_callback_monotonically(tmp_path: Path) -> None:
    reported = collect_progress(make_media(tmp_path, DETECTOR_STDERR), tmp_path)

    assert reported == sorted(reported)
    assert reported[0] > 0
    # Capped below 100: the segmentation maths and the clip-list write still
    # follow the last analysed frame, so the bar must not claim to be finished.
    assert reported[-1] == 95.0


def test_lines_without_a_reading_report_nothing(tmp_path: Path) -> None:
    quiet = ["[human-segments] Loading detector...", "[human-segments] sampling at 1.0 fps"]

    assert collect_progress(make_media(tmp_path, quiet), tmp_path) == []


def test_every_output_line_is_still_logged(tmp_path: Path) -> None:
    # Progress parsing is additive: the live log panel renders this stream.
    media = make_media(tmp_path, DETECTOR_STDERR)
    run_detection(media, tmp_path)

    logged = [
        payload["message"]
        for _, event, payload in media.events.items
        if event == "log" and payload.get("source") == "human-detector"
    ]
    assert logged == DETECTOR_STDERR


def test_progress_events_are_published_for_the_detection_step(tmp_path: Path) -> None:
    media = make_media(tmp_path, DETECTOR_STDERR)
    run_detection(media, tmp_path)

    progress = [payload for _, event, payload in media.events.items if event == "progress"]
    assert progress, "the detector must publish progress events"
    assert {entry["step"] for entry in progress} == {"person_detection"}
    assert progress[-1]["percent"] == 95.0


def test_detection_still_works_without_a_progress_callback(tmp_path: Path) -> None:
    # The output handler is installed unconditionally now; a caller that wants
    # no readings must not be a crash.
    result = run_detection(make_media(tmp_path, DETECTOR_STDERR), tmp_path)

    assert len(result["clipRanges"]) == 2
    assert result["source"]["type"] == "person_detection_rtdetr"
