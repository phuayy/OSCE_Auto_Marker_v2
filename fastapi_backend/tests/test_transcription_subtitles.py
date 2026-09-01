"""Subtitle rendering for engines that write none of their own.

WhisperX emits SRT and VTT itself. Canary-Qwen returns segments only, so these
files are rendered from them — and the workspace video player, the download
links and the corpus-correction rewrite all read them, so the cue numbering and
timestamp format have to match what WhisperX would have written.
"""
from __future__ import annotations

from app.pipeline.transcription.subtitles import (
    build_srt_text,
    convert_srt_text_to_vtt_text,
    format_srt_timestamp,
)

SEGMENTS = [
    {"start": 0.0, "end": 2.5, "speaker": "SPEAKER_00", "text": "Good morning, I am the medical student."},
    {"start": 2.5, "end": 4.0, "speaker": "SPEAKER_01", "text": "Morning."},
]


def test_timestamps_use_the_srt_comma_format() -> None:
    assert format_srt_timestamp(0) == "00:00:00,000"
    assert format_srt_timestamp(2.5) == "00:00:02,500"
    assert format_srt_timestamp(3661.125) == "01:01:01,125"


def test_a_negative_offset_clamps_to_zero() -> None:
    # SRT cannot express one, and a player rejects the whole file if it sees it.
    assert format_srt_timestamp(-1.0) == "00:00:00,000"


def test_cues_are_numbered_from_one_and_carry_the_speaker() -> None:
    srt = build_srt_text(SEGMENTS)

    assert srt.startswith("1\n00:00:00,000 --> 00:00:02,500\n[SPEAKER_00] Good morning")
    assert "2\n00:00:02,500 --> 00:00:04,000\n[SPEAKER_01] Morning." in srt


def test_an_unlabelled_segment_renders_without_a_speaker_prefix() -> None:
    srt = build_srt_text([{"start": 0.0, "end": 1.0, "text": "Hello."}])

    assert "[" not in srt
    assert srt.strip().endswith("Hello.")


def test_empty_segments_are_skipped_and_numbering_stays_contiguous() -> None:
    srt = build_srt_text(
        [
            {"start": 0.0, "end": 1.0, "text": "One."},
            {"start": 1.0, "end": 2.0, "text": "   "},
            {"start": 2.0, "end": 3.0, "text": "Two."},
            "not-a-segment",
        ]
    )

    assert "1\n" in srt and "2\n" in srt and "3\n" not in srt
    assert "Two." in srt


def test_a_reversed_cue_is_clamped_rather_than_dropped() -> None:
    # The text is still evidence a scorer may need; players tolerate a
    # zero-length cue but choke on an end before its start.
    srt = build_srt_text([{"start": 5.0, "end": 3.0, "text": "Late."}])

    assert "00:00:05,000 --> 00:00:05,000" in srt


def test_a_missing_end_falls_back_to_the_start() -> None:
    srt = build_srt_text([{"start": 4.0, "text": "No end."}])

    assert "00:00:04,000 --> 00:00:04,000" in srt


def test_no_usable_segments_yields_an_empty_file() -> None:
    assert build_srt_text([]) == ""
    assert build_srt_text([{"text": ""}]) == ""


def test_vtt_conversion_adds_the_header_and_dot_separators() -> None:
    vtt = convert_srt_text_to_vtt_text(build_srt_text(SEGMENTS))

    assert vtt.startswith("WEBVTT\n\n")
    assert "00:00:00.000 --> 00:00:02.500" in vtt
    # Commas inside the dialogue must survive; only the cue line changes.
    assert "Good morning, I am the medical student." in vtt


def test_vtt_conversion_matches_the_whisperx_converter() -> None:
    # Both converters feed the same <track> element, so they must agree.
    from app.pipeline.media import MediaPipeline

    srt = build_srt_text(SEGMENTS)

    assert convert_srt_text_to_vtt_text(srt) == MediaPipeline.convert_srt_text_to_vtt_text(srt)
