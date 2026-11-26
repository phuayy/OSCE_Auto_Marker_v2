"""Pure stitching of Canary-Qwen's overlapping windows into sentence segments.

The model decodes each window independently, so the 2 s overlap is transcribed
twice and a window is one 30 s segment. The script's pure helpers remove the
repeated words at each seam and cut the text into sentences with interpolated
times, so the pyannote overlap assignment can label them one by one. No NeMo or
torch is needed here.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "canary_qwen_transcribe.py"
_spec = importlib.util.spec_from_file_location("canary_qwen_transcribe_stitching", _SCRIPT)
canary = importlib.util.module_from_spec(_spec)
sys.modules.setdefault(_spec.name, canary)
_spec.loader.exec_module(canary)

Window = canary.WindowText
stitch = canary.stitch_windows
OVERLAP = 2.0


def joined(segments: list[dict]) -> str:
    return " ".join(segment["text"] for segment in segments)


def assert_well_formed(segments: list[dict], windows: list) -> None:
    low = min(window.start for window in windows)
    high = max(window.end for window in windows)
    previous_end = low
    for segment in segments:
        assert low <= segment["start"] <= segment["end"] <= high
        assert segment["start"] >= previous_end - 1e-6, "segments must not run backwards"
        previous_end = segment["end"]


def test_identical_overlap_is_removed_once() -> None:
    windows = [
        Window(0.0, 30.0, "Hello there. How long have you had the pain in your chest"),
        Window(28.0, 58.0, "pain in your chest. It started yesterday evening."),
    ]
    segments = stitch(windows, OVERLAP)

    assert joined(segments) == "Hello there. How long have you had the pain in your chest. It started yesterday evening."
    assert_well_formed(segments, windows)


def test_slightly_different_decoding_of_the_overlap_is_still_one_copy() -> None:
    windows = [
        Window(0.0, 30.0, "I will check your blood pressure and your heart rate now"),
        Window(28.0, 58.0, "your blood pressure and you're heart rate now. Please sit back."),
    ]
    segments = stitch(windows, OVERLAP)

    text = joined(segments)
    assert text.count("blood pressure") == 1
    assert text.endswith("Please sit back.")
    assert_well_formed(segments, windows)


def test_a_full_stop_heard_only_by_the_later_window_is_kept() -> None:
    windows = [
        Window(0.0, 30.0, "It started two days ago and got worse"),
        Window(28.0, 58.0, "and got worse. Any fever?"),
    ]
    segments = stitch(windows, OVERLAP)

    assert joined(segments) == "It started two days ago and got worse. Any fever?"


def test_no_overlap_leaves_both_texts_intact() -> None:
    windows = [
        Window(0.0, 30.0, "First window ends here."),
        Window(30.0, 60.0, "Second window starts afresh."),
    ]
    segments = stitch(windows, 0.0)

    assert joined(segments) == "First window ends here. Second window starts afresh."


def test_unrelated_head_is_not_trimmed() -> None:
    windows = [
        Window(0.0, 30.0, "Do you smoke or drink alcohol"),
        Window(28.0, 58.0, "Any allergies to medication? None that I know of."),
    ]
    segments = stitch(windows, OVERLAP)

    assert joined(segments) == "Do you smoke or drink alcohol Any allergies to medication? None that I know of."


def test_a_genuine_single_word_repeat_is_not_swallowed() -> None:
    windows = [
        Window(0.0, 30.0, "Is it getting worse? No"),
        Window(28.0, 58.0, "No, it is about the same."),
    ]
    segments = stitch(windows, OVERLAP)

    assert joined(segments).count("No") == 2


def test_last_window_wholly_inside_the_overlap_adds_nothing() -> None:
    windows = [
        Window(0.0, 30.0, "Thank you for coming in today, doctor"),
        Window(29.0, 31.0, "today, doctor"),
    ]
    segments = stitch(windows, OVERLAP)

    assert joined(segments) == "Thank you for coming in today, doctor"
    assert_well_formed(segments, windows)


def test_empty_windows_are_skipped_and_break_the_seam() -> None:
    windows = [
        Window(0.0, 30.0, "Good morning. Take a seat."),
        Window(28.0, 58.0, ""),
        Window(56.0, 86.0, "Take a seat. Tell me more."),
    ]
    segments = stitch(windows, OVERLAP)

    # The empty window between them means the two are not neighbours, so no
    # text is borrowed across the gap.
    assert joined(segments) == "Good morning. Take a seat. Take a seat. Tell me more."
    assert_well_formed(segments, windows)


def test_everything_empty_gives_no_segments() -> None:
    assert stitch([Window(0.0, 30.0, ""), Window(28.0, 58.0, "  ")], OVERLAP) == []
    assert stitch([], OVERLAP) == []


def test_a_window_is_cut_into_sentences_with_interpolated_times() -> None:
    windows = [Window(10.0, 40.0, "Hello. How are you feeling today? I have a headache!")]
    segments = stitch(windows, OVERLAP)

    assert [segment["text"] for segment in segments] == [
        "Hello.",
        "How are you feeling today?",
        "I have a headache!",
    ]
    assert segments[0]["start"] == 10.0 and segments[-1]["end"] == 40.0
    assert segments[0]["end"] <= segments[1]["start"] + 1e-6
    # Longer sentences take more of the window.
    durations = [segment["end"] - segment["start"] for segment in segments]
    assert durations[1] > durations[0]
    assert_well_formed(segments, windows)


def test_abbreviations_and_decimals_do_not_end_a_sentence() -> None:
    windows = [Window(0.0, 30.0, "Dr. Smith will see you. The dose is 2.5 mg twice daily.")]
    segments = stitch(windows, OVERLAP)

    assert [segment["text"] for segment in segments] == [
        "Dr. Smith will see you.",
        "The dose is 2.5 mg twice daily.",
    ]


def test_a_single_long_sentence_stays_one_segment_inside_its_window() -> None:
    text = " ".join(["and"] * 5 + ["then", "the", "patient", "said"] * 10)
    windows = [Window(5.0, 35.0, text)]
    segments = stitch(windows, OVERLAP)

    assert len(segments) == 1
    assert segments[0]["text"] == text
    assert segments[0]["start"] == 5.0 and segments[0]["end"] == 35.0


def test_trimmed_head_moves_the_segment_start_past_the_overlap() -> None:
    windows = [
        Window(0.0, 30.0, "One two three four five six seven eight nine ten."),
        Window(28.0, 58.0, "nine ten. Eleven twelve thirteen fourteen fifteen."),
    ]
    segments = stitch(windows, OVERLAP)

    assert joined(segments) == "One two three four five six seven eight nine ten. Eleven twelve thirteen fourteen fifteen."
    assert segments[-1]["start"] >= 30.0 - 1e-6
    assert_well_formed(segments, windows)


def test_three_windows_chain_without_duplicates_and_stay_monotonic() -> None:
    windows = [
        Window(0.0, 30.0, "Alpha bravo charlie delta echo foxtrot golf."),
        Window(28.0, 58.0, "foxtrot golf. Hotel india juliet kilo lima."),
        Window(56.0, 86.0, "kilo lima. Mike november oscar papa."),
    ]
    segments = stitch(windows, OVERLAP)

    assert joined(segments) == (
        "Alpha bravo charlie delta echo foxtrot golf. Hotel india juliet kilo lima. Mike november oscar papa."
    )
    assert_well_formed(segments, windows)
