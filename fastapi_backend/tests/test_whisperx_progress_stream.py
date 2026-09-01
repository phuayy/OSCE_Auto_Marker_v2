"""Live WhisperX progress from the CLI flag through to the session record.

Covers the three seams: the ``--print_progress`` flag, the output handler that
lifts a percentage out of stdout without disturbing the log stream, and the
pipeline step state the session-list projection reads.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.test_whisperx_invocation import make_media, run_transcription

TRANSCRIBE_PHASE = ["Progress: 25.00%...", "Progress: 50.00%...", "Progress: 100.00%..."]
ALIGN_PHASE = ["Progress: 50.00%...", "Progress: 100.00%..."]


def collect_progress(media: Any, tmp_path: Path) -> list[float]:
    reported: list[float] = []

    async def on_progress(percent: float) -> None:
        reported.append(percent)

    run_transcription(media, tmp_path, on_progress=on_progress)
    return reported


def test_print_progress_flag_is_passed_by_default(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path)
    run_transcription(media, tmp_path)

    args = runner.whisperx_args()
    assert args[args.index("--print_progress") + 1] == "True"


def test_print_progress_can_be_turned_off(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path, whisperx_print_progress=False)
    run_transcription(media, tmp_path)

    assert "--print_progress" not in runner.whisperx_args()


def test_progress_lines_reach_the_callback_monotonically(tmp_path: Path) -> None:
    media, _ = make_media(tmp_path, whisperx_stdout=TRANSCRIBE_PHASE + ALIGN_PHASE)

    reported = collect_progress(media, tmp_path)

    assert reported == sorted(reported)
    assert reported[0] > 0
    assert reported[-1] == 90.0


def test_every_output_line_is_still_logged(tmp_path: Path) -> None:
    # Progress parsing is additive: the raw log stream must be unchanged, since
    # it is what the session workspace's live log panel renders.
    lines = ["Performing transcription...", "Progress: 50.00%..."]
    media, _ = make_media(tmp_path, whisperx_stdout=lines)

    run_transcription(media, tmp_path)

    logged = [
        payload["message"]
        for _, event, payload in media.events.items
        if event == "log" and str(payload.get("source", "")).startswith("whisperx-std")
    ]
    assert logged == lines


def test_progress_events_are_published_for_the_whisperx_step(tmp_path: Path) -> None:
    media, _ = make_media(tmp_path, whisperx_stdout=TRANSCRIBE_PHASE)

    run_transcription(media, tmp_path)

    progress_events = [payload for _, event, payload in media.events.items if event == "progress"]
    assert progress_events
    assert {payload["step"] for payload in progress_events} == {"whisperx"}
    assert all(0 <= payload["percent"] <= 100 for payload in progress_events)


def test_runs_without_a_progress_callback(tmp_path: Path) -> None:
    # The callback is optional; clip re-runs and cached paths pass nothing.
    media, _ = make_media(tmp_path, whisperx_stdout=TRANSCRIBE_PHASE)

    outputs = run_transcription(media, tmp_path)

    assert outputs["jsonAbsolutePath"] is not None


def test_no_progress_lines_means_no_callbacks(tmp_path: Path) -> None:
    media, _ = make_media(tmp_path, whisperx_stdout=["Performing transcription...", "done"])

    assert collect_progress(media, tmp_path) == []
