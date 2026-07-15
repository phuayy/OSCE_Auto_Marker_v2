#!/usr/bin/env python3
"""Self-check for ``rt_detr.build_tolerant_session_ranges`` (pure function,
no GPU/model/video needed).

Validates the exact scenario from the request: a 50-sample confirmation
window containing 40 correct 2-person samples and 10 misclassified
1-person samples (scattered as short on/off bursts — a model bottleneck)
must still confirm as ONE session, anchored to the first 2-person sample.
A genuine short blip must NOT confirm. The same tolerance must apply
symmetrically at the end (clip cut-off) boundary.

Run: OSCE-AI-FYP/.venv/Scripts/python.exe debug_scripts/test_segmentation_tuning.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rt_detr import build_tolerant_session_ranges  # noqa: E402

TUNED = dict(
    min_people=2,
    sample_dt=1.0,  # 1 sample fps -> 1 sample == 1 second, easiest to reason about
    start_window_samples=50,
    start_tolerance_samples=10,
    end_window_samples=50,
    end_tolerance_samples=10,
    start_offset_seconds=0.0,
    end_offset_seconds=0.0,
    min_clip_seconds=0.5,
)


def make_bottleneck_pattern(good: int = 40, bad: int = 10, burst: int = 3, good_run: int = 6) -> list[int]:
    """`good` x '2 people' + `bad` x '1 person', interleaved as short on/off bursts."""
    counts: list[int] = []
    good_left, bad_left = good, bad
    while good_left > 0 or bad_left > 0:
        run_good = min(good_run, good_left)
        counts += [2] * run_good
        good_left -= run_good
        if bad_left > 0:
            run_bad = min(burst, bad_left)
            counts += [1] * run_bad
            bad_left -= run_bad
    return counts


def test_bottleneck_window_confirms_as_single_session() -> None:
    idle_lead = [0] * 5
    block = make_bottleneck_pattern()
    assert len(block) == 50 and block.count(2) == 40 and block.count(1) == 10
    active_tail = [2] * 10
    idle_trail = [0] * 55  # > end window so the session actually closes
    counts = idle_lead + block + active_tail + idle_trail

    ranges, _transitions = build_tolerant_session_ranges(counts, video_duration=float(len(counts)), **TUNED)

    assert len(ranges) == 1, f"expected exactly one confirmed session, got {ranges}"
    clip = ranges[0]
    assert clip["start"] == 5.0, f"start not anchored to the first 2-person frame: {clip}"
    expected_min_end = float(len(idle_lead) + len(block) + len(active_tail))
    assert clip["end"] >= expected_min_end, f"session closed before the sustained departure: {clip}"


def test_short_blip_is_rejected() -> None:
    """A brief 2-person blip shorter than the confirmation window must not open a session."""
    counts = [0] * 20 + [2] * 5 + [0] * 60
    ranges, _transitions = build_tolerant_session_ranges(counts, video_duration=float(len(counts)), **TUNED)
    assert ranges == [], f"a 5-frame blip must not confirm a session, got {ranges}"


def test_too_much_misclassification_is_rejected() -> None:
    """More bad samples than the tolerance allows must NOT confirm a session."""
    block = make_bottleneck_pattern(good=35, bad=15, burst=3)  # 15 > tolerance(10)
    counts = [0] * 5 + block + [0] * 60
    ranges, _transitions = build_tolerant_session_ranges(counts, video_duration=float(len(counts)), **TUNED)
    assert ranges == [], f"15 bad samples in a 50-window exceeds tolerance, must not confirm: {ranges}"


def test_departure_burst_does_not_split_session() -> None:
    """The same tolerance must apply symmetrically at the end (cut-off) boundary."""
    active_lead = [2] * 60
    mixed_departure = []
    toggle = True
    remaining_bad = 10
    while len(mixed_departure) < 50:
        if toggle and remaining_bad > 0:
            run = min(3, remaining_bad)
            mixed_departure += [1] * run
            remaining_bad -= run
        else:
            mixed_departure += [2] * 3
        toggle = not toggle
    real_departure = [0] * 55
    counts = active_lead + mixed_departure + real_departure

    ranges, _transitions = build_tolerant_session_ranges(counts, video_duration=float(len(counts)), **TUNED)
    assert len(ranges) == 1, f"a transient drop mid-session must not split it, got {ranges}"
    clip = ranges[0]
    assert clip["end"] >= float(len(active_lead) + len(mixed_departure)), (
        f"session closed too early on a transient drop: {clip}"
    )


if __name__ == "__main__":
    test_bottleneck_window_confirms_as_single_session()
    test_short_blip_is_rejected()
    test_too_much_misclassification_is_rejected()
    test_departure_burst_does_not_split_session()
    print("OK — segmentation tuning self-check passed (4/4).")
