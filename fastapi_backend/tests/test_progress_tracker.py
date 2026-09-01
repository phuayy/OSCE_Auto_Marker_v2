"""The pure parser behind live WhisperX progress.

WhisperX prints ``Progress: 42.10%...`` from two independent loops
(transcription, then alignment), each counting 0-100, and prints nothing during
diarisation. These tests pin the mapping from those raw readings onto one
never-decreasing step percentage.
"""
from __future__ import annotations

from app.pipeline.progress_tracker import ProgressTracker


def feed(tracker: ProgressTracker, *lines: str) -> list[float]:
    """Every percentage the tracker reported for the given lines."""
    return [percent for percent in (tracker.update(line) for line in lines) if percent is not None]


def test_parse_reads_the_whisperx_line_format() -> None:
    assert ProgressTracker.parse("Progress: 42.10%...") == 42.10
    assert ProgressTracker.parse("Progress: 100.00%...") == 100.0
    assert ProgressTracker.parse("Progress: 7%") == 7.0


def test_parse_ignores_lines_without_a_reading() -> None:
    assert ProgressTracker.parse("Performing transcription...") is None
    assert ProgressTracker.parse("") is None
    assert ProgressTracker.parse(None) is None


def test_first_phase_maps_onto_the_first_span() -> None:
    tracker = ProgressTracker()

    assert feed(tracker, "Progress: 0.00%...") == [0.0]
    assert feed(tracker, "Progress: 50.00%...") == [22.5]
    assert feed(tracker, "Progress: 100.00%...") == [45.0]


def test_a_restarting_counter_advances_to_the_next_phase() -> None:
    # Alignment counts from zero again; the bar must not rewind to zero with it.
    tracker = ProgressTracker()
    feed(tracker, "Progress: 100.00%...")

    assert feed(tracker, "Progress: 10.00%...") == [49.5]
    assert feed(tracker, "Progress: 100.00%...") == [90.0]


def test_an_out_of_order_line_is_not_read_as_a_new_phase() -> None:
    tracker = ProgressTracker()
    feed(tracker, "Progress: 80.00%...", "Progress: 100.00%...")

    # A small dip is a straggler, not the alignment loop starting over; reading
    # it as a restart would jump the bar into the next phase near its end.
    assert feed(tracker, "Progress: 99.00%...") == []
    assert tracker.percent == 45.0


def test_progress_never_decreases() -> None:
    tracker = ProgressTracker()
    seen: list[float] = []
    for raw in (10.0, 60.0, 100.0, 20.0, 55.0, 100.0, 90.0):
        tracker.update(f"Progress: {raw:.2f}%...")
        seen.append(tracker.percent)

    assert seen == sorted(seen)
    assert seen[-1] == 90.0


def test_final_phase_stops_short_of_completion_for_the_diarisation_tail() -> None:
    tracker = ProgressTracker()
    feed(tracker, "Progress: 100.00%...", "Progress: 5.00%...", "Progress: 100.00%...")

    # Diarisation reports nothing; the step is only 100% once it is marked done.
    assert tracker.percent == 90.0


def test_small_advances_are_not_reported() -> None:
    tracker = ProgressTracker(min_delta=2.0)
    feed(tracker, "Progress: 0.00%...")

    # 1% of phase one is 0.45 points overall — below the reporting threshold.
    assert tracker.update("Progress: 1.00%...") is None
    assert tracker.update("Progress: 2.00%...") is None
    assert tracker.update("Progress: 10.00%...") == 4.5


def test_phase_end_is_always_reported_even_below_the_threshold() -> None:
    tracker = ProgressTracker(min_delta=50.0)
    feed(tracker, "Progress: 0.00%...")

    assert tracker.update("Progress: 100.00%...") == 45.0


def test_repeated_identical_readings_report_once() -> None:
    tracker = ProgressTracker()

    assert tracker.update("Progress: 20.00%...") == 9.0
    assert tracker.update("Progress: 20.00%...") is None


def test_out_of_range_readings_are_clamped() -> None:
    tracker = ProgressTracker()

    assert ProgressTracker.parse("Progress: 140.00%...") == 100.0
    assert feed(tracker, "Progress: 140.00%...") == [45.0]


def test_phase_spans_are_configurable() -> None:
    tracker = ProgressTracker(phase_spans=((0.0, 100.0),), min_delta=0.0)

    assert feed(tracker, "Progress: 25.00%...") == [25.0]
    # With one span configured, a restart cannot escape it.
    assert feed(tracker, "Progress: 100.00%...", "Progress: 1.00%...") == [100.0]
