"""Speaker assignment for engines that return plain text.

The scorers read speaker-tagged dialogue, so an engine without diarisation gets
a standalone pyannote pass whose turns are merged onto its segments. A wrong
attribution silently moves a student's words onto the simulated patient, so the
merge prefers an admitted unknown over a guess.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings
from app.core.process import CommandResult
from app.pipeline.transcription.diarization import (
    DIARIZATION_SCHEMA,
    PyannoteDiarizer,
    assign_speakers,
    overlap_seconds,
    write_turns_file,
)

TURNS = [
    {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
    {"start": 5.0, "end": 9.0, "speaker": "SPEAKER_01"},
]


def test_overlap_of_disjoint_intervals_is_zero() -> None:
    assert overlap_seconds(0.0, 1.0, 2.0, 3.0) == 0
    assert overlap_seconds(0.0, 2.0, 1.0, 3.0) == 1.0
    assert overlap_seconds(1.0, 3.0, 0.0, 10.0) == 2.0


def test_each_segment_takes_the_speaker_it_overlaps_most() -> None:
    segments = [
        {"start": 0.0, "end": 4.0, "text": "Good morning."},
        {"start": 5.5, "end": 8.0, "text": "Morning, doctor."},
    ]

    labelled = assign_speakers(segments, TURNS)

    assert labelled == 2
    assert [segment["speaker"] for segment in segments] == ["SPEAKER_00", "SPEAKER_01"]


def test_a_segment_spanning_a_turn_boundary_takes_the_larger_share() -> None:
    segments = [{"start": 4.0, "end": 8.0, "text": "…"}]  # 1s of 00, 3s of 01

    assign_speakers(segments, TURNS)

    assert segments[0]["speaker"] == "SPEAKER_01"


def test_a_segment_overlapping_nothing_keeps_its_existing_label() -> None:
    # Borrowing a neighbour's speaker would attribute words to someone who
    # demonstrably was not speaking then.
    segments = [{"start": 20.0, "end": 22.0, "text": "…", "speaker": "SPEAKER_UNKNOWN"}]

    labelled = assign_speakers(segments, TURNS)

    assert labelled == 0
    assert segments[0]["speaker"] == "SPEAKER_UNKNOWN"


def test_no_turns_means_nothing_is_relabelled() -> None:
    segments = [{"start": 0.0, "end": 1.0, "text": "…"}]

    assert assign_speakers(segments, []) == 0
    assert "speaker" not in segments[0]


def test_malformed_turns_and_segments_are_ignored() -> None:
    segments = [{"start": 0.0, "end": 4.0, "text": "…"}, "not-a-segment"]
    turns = [*TURNS, {"start": None, "end": 3.0, "speaker": "SPEAKER_09"}, {"speaker": "SPEAKER_08"}]

    assert assign_speakers(segments, turns) == 1
    assert segments[0]["speaker"] == "SPEAKER_00"


def test_ties_resolve_deterministically() -> None:
    segments = [{"start": 4.0, "end": 6.0, "text": "…"}]  # exactly 1s each side

    assign_speakers(segments, TURNS)
    first = segments[0]["speaker"]
    assign_speakers(segments, list(reversed(TURNS)))

    assert segments[0]["speaker"] == first


def test_a_segment_without_an_end_is_treated_as_an_instant() -> None:
    segments = [{"start": 6.0, "text": "…"}]

    assign_speakers(segments, TURNS)

    assert segments[0].get("speaker") is None or segments[0]["speaker"] == "SPEAKER_01"


# --- the subprocess wrapper -------------------------------------------------


class FakeRunner:
    def __init__(self, settings: Settings, turns: list[dict[str, Any]] | None) -> None:
        self.settings = settings
        self.turns = turns
        self.args: list[str] = []

    async def run(self, command: str, args: list[str], _label: str, **_kwargs: Any) -> CommandResult:
        self.args = list(args)
        if self.turns is not None:
            write_turns_file(Path(args[args.index("--output") + 1]), self.turns)
        return CommandResult(stdout="", stderr="")


class FakeEvents:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict[str, Any]]] = []

    async def publish(self, session_id: str, event: str, payload: dict[str, Any]) -> None:
        self.items.append((session_id, event, payload))


def stub_script_root(tmp_path: Path) -> Path:
    """A project root whose scripts/ exists but whose storage stays in tmp.

    The subprocess boundary is faked in these tests, so the scripts only need
    to *exist* — pointing root_dir at the real repository would make the
    pipeline write its artifacts into the developer's storage directory.
    """
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    for name in ("pyannote_diarize.py", "canary_qwen_transcribe.py"):
        (scripts_dir / name).write_text("# test stub", encoding="utf-8")
    return tmp_path


def build_diarizer(tmp_path: Path, turns: list[dict[str, Any]] | None) -> tuple[PyannoteDiarizer, FakeRunner]:
    settings = Settings(
        root_dir=stub_script_root(tmp_path),
        backend_root=tmp_path,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
    )
    runner = FakeRunner(settings, turns)
    auth = SimpleNamespace(runtime=SimpleNamespace(whisperx_hf_token="hf-token"))
    return PyannoteDiarizer(settings, runner, FakeEvents(), auth), runner


def test_the_diarizer_forwards_the_speaker_bounds(tmp_path: Path) -> None:
    diarizer, runner = build_diarizer(tmp_path, TURNS)

    turns = asyncio.run(
        diarizer.run("session-1", tmp_path / "audio.wav", tmp_path / "turns.json", min_speakers=2, max_speakers=2)
    )

    assert turns == TURNS
    assert runner.args[runner.args.index("--min-speakers") + 1] == "2"
    assert runner.args[runner.args.index("--max-speakers") + 1] == "2"
    assert runner.args[runner.args.index("--model") + 1].startswith("pyannote/")


def test_unset_bounds_are_omitted(tmp_path: Path) -> None:
    diarizer, runner = build_diarizer(tmp_path, TURNS)

    asyncio.run(diarizer.run("session-1", tmp_path / "audio.wav", tmp_path / "turns.json"))

    assert "--min-speakers" not in runner.args
    assert "--max-speakers" not in runner.args


def test_a_missing_output_file_is_an_error(tmp_path: Path) -> None:
    diarizer, _ = build_diarizer(tmp_path, None)

    with pytest.raises(RuntimeError, match="no output file"):
        asyncio.run(diarizer.run("session-1", tmp_path / "audio.wav", tmp_path / "turns.json"))


def test_an_unexpected_schema_is_an_error(tmp_path: Path) -> None:
    diarizer, _ = build_diarizer(tmp_path, TURNS)
    output_path = tmp_path / "turns.json"

    class WrongSchemaRunner:
        async def run(self, _command: str, args: list[str], _label: str, **_kwargs: Any) -> CommandResult:
            Path(args[args.index("--output") + 1]).write_text(json.dumps({"schema": "other", "turns": []}), "utf-8")
            return CommandResult(stdout="", stderr="")

    diarizer.runner = WrongSchemaRunner()

    with pytest.raises(RuntimeError, match="unexpected schema"):
        asyncio.run(diarizer.run("session-1", tmp_path / "audio.wav", output_path))


def test_the_huggingface_token_reaches_the_subprocess(tmp_path: Path) -> None:
    # pyannote's models are gated; without the token the script cannot load one.
    diarizer, _ = build_diarizer(tmp_path, TURNS)

    assert diarizer.python_env()["HF_TOKEN"] == "hf-token"


def test_written_turn_files_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "turns.json"

    write_turns_file(path, TURNS)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["schema"] == DIARIZATION_SCHEMA
    assert payload["turns"] == TURNS
