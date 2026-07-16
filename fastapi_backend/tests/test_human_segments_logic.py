"""Unit tests for the pure segmentation maths in scripts/detect_human_segments.py.

Covers the tolerant N-of-M segmenter (50-sample window / 10 tolerated
misclassifications), the full-timeline partition (sessions + intermissions),
and the multi-worker chunk split. The script is loaded the same way
test_rubric_section.py loads its script (importlib, no sys.path pollution).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "detect_human_segments.py"
_spec = importlib.util.spec_from_file_location("detect_human_segments", _SCRIPT)
dhs = importlib.util.module_from_spec(_spec)
# Register before exec: the script's @dataclass decorators resolve their own
# module through sys.modules at class-creation time.
sys.modules[_spec.name] = dhs
_spec.loader.exec_module(dhs)

_KWARGS = dict(
    min_people=2,
    sample_dt=1.0,
    start_window_samples=50,
    start_tolerance_samples=10,
    end_window_samples=50,
    end_tolerance_samples=10,
    start_offset_seconds=0.0,
    end_offset_seconds=0.0,
    min_clip_seconds=0.5,
)


def test_tolerant_segmenter_confirms_session_despite_flicker() -> None:
    # 100s idle, 100s of 2 people with 8 mid-session dropouts, 100s idle.
    counts = [0] * 100 + [2] * 100 + [0] * 100
    for miss in (110, 125, 140, 155, 160, 170, 175, 180):
        counts[miss] = 1
    ranges, transitions = dhs.build_tolerant_session_ranges(counts, 300.0, **_KWARGS)
    assert [(r["start"], r["end"], r["student_index"]) for r in ranges] == [(100.0, 200.0, 1)]
    events = [t["event"] for t in transitions]
    assert events == ["session_start", "session_end"]


def test_tolerant_segmenter_rejects_noise_beyond_tolerance() -> None:
    # Only every third sample shows 2 people — 2/3 of every window disagrees.
    counts = [2 if i % 3 == 0 else 0 for i in range(300)]
    ranges, _ = dhs.build_tolerant_session_ranges(counts, 300.0, **_KWARGS)
    assert ranges == []


def test_tolerant_segmenter_closes_session_at_eof() -> None:
    counts = [0] * 50 + [2] * 100  # video ends mid-session
    ranges, transitions = dhs.build_tolerant_session_ranges(counts, 150.0, **_KWARGS)
    assert len(ranges) == 1
    assert ranges[0]["start"] == 50.0 and ranges[0]["end"] == 150.0
    assert transitions[-1]["event"] == "session_end_at_eof"


def test_timeline_partition_is_gapless_and_classifies_gaps() -> None:
    counts = [0] * 100 + [2] * 100 + [1] * 100 + [2] * 100 + [0] * 50
    ranges, _ = dhs.build_tolerant_session_ranges(counts, 450.0, **_KWARGS)
    assert len(ranges) == 2
    segments = dhs.build_timeline_segments(counts, ranges, 450.0, 1.0)

    # Gapless ordered cover of [0, duration].
    assert segments[0]["start"] == 0.0
    assert segments[-1]["end"] == 450.0
    for left, right in zip(segments, segments[1:]):
        assert left["end"] == right["start"]

    kinds = [segment["kind"] for segment in segments]
    assert kinds == ["intermission", "session", "intermission", "session", "intermission"]
    # Gap classification: empty room before, one person between, empty after.
    assert segments[0]["person_count"] == 0
    assert segments[2]["person_count"] == 1
    assert segments[4]["person_count"] == 0
    # Sessions carry their student index.
    assert [segment["student_index"] for segment in segments if segment["kind"] == "session"] == [1, 2]


def test_timeline_partition_without_sessions_is_one_intermission() -> None:
    segments = dhs.build_timeline_segments([1] * 60, [], 60.0, 1.0)
    assert segments == [{"start": 0.0, "end": 60.0, "kind": "intermission", "person_count": 1}]


def test_split_sample_indices_is_contiguous_cover() -> None:
    for total, workers in [(10, 2), (11, 3), (1, 4), (100, 1), (5, 100)]:
        spans = dhs.split_sample_indices(total, workers)
        assert all(a < b for a, b in spans)
        assert [i for a, b in spans for i in range(a, b)] == list(range(total))
    assert dhs.split_sample_indices(0, 2) == []
