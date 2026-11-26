"""Which diarised speaker the audio-professionalism scorer measures as the student."""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audio_professionalism_extractor as extractor  # noqa: E402


def seg(start: float, end: float, speaker: str, text: str) -> dict:
    return {"start": start, "end": end, "speaker": speaker, "text": text}


def test_patient_who_talks_more_is_not_chosen_as_the_student():
    segments = [
        seg(0, 4, "SPEAKER_01", "Hello, I'm the student. What brings you in today?"),
        seg(4, 40, "SPEAKER_00", "Well it started on Monday and it has been getting worse and worse."),
        seg(40, 44, "SPEAKER_01", "How long has the pain lasted? Does it spread anywhere?"),
        seg(44, 90, "SPEAKER_00", "It goes to my back, and I feel sick when I eat, I can't sleep either."),
        seg(90, 93, "SPEAKER_01", "Any allergies?"),
    ]
    choice = extractor.select_student_speaker(segments, None)
    assert choice["id"] == "SPEAKER_01"
    assert choice["selection"] == "most_questions"
    assert "questions" in choice["reason"]
    by_id = {entry["id"]: entry for entry in choice["speakers"]}
    assert by_id["SPEAKER_00"]["talk_seconds"] > by_id["SPEAKER_01"]["talk_seconds"]
    assert by_id["SPEAKER_01"]["questions"] == 4


def test_no_questions_falls_back_to_who_opened_the_station_with_low_confidence():
    segments = [
        seg(5, 9, "SPEAKER_01", "Hello, I am the student."),
        seg(9, 60, "SPEAKER_00", "I have had a cough."),
    ]
    choice = extractor.select_student_speaker(segments, None)
    assert choice["id"] == "SPEAKER_01"
    assert choice["selection"] == "first_speaker"
    assert choice["confidence"] < 0.3


def test_equal_question_counts_is_a_tie_decided_by_the_opener():
    segments = [
        seg(0, 3, "SPEAKER_00", "Is it serious? Will it go?"),
        seg(3, 6, "SPEAKER_01", "What is the pain like? Where is it?"),
    ]
    choice = extractor.select_student_speaker(segments, None)
    assert choice["selection"] == "first_speaker"
    assert choice["id"] == "SPEAKER_00"


def test_a_single_stray_patient_question_is_not_enough_evidence():
    segments = [
        seg(0, 3, "SPEAKER_01", "Hello."),
        seg(3, 30, "SPEAKER_00", "Is it serious?"),
    ]
    choice = extractor.select_student_speaker(segments, None)
    assert choice["selection"] == "first_speaker"
    assert choice["id"] == "SPEAKER_01"


def test_single_speaker_is_chosen_and_flagged_for_verification():
    choice = extractor.select_student_speaker([seg(0, 10, "SPEAKER_00", "Hello. How are you?")], None)
    assert choice["id"] == "SPEAKER_00"
    assert choice["selection"] == "single_speaker"
    assert choice["confidence"] < 0.3


def test_unlabelled_transcript_is_one_unknown_speaker():
    segments = [seg(0, 10, extractor.infer_speaker({"text": "x"}), "Hello. How are you?")]
    choice = extractor.select_student_speaker(segments, None)
    assert choice["id"] == "SPEAKER_UNKNOWN"
    assert choice["selection"] == "no_speaker_labels"
    assert choice["confidence"] == 0.0


def test_empty_transcript_and_override():
    assert extractor.select_student_speaker([], None)["selection"] == "fallback"
    override = extractor.select_student_speaker([seg(0, 1, "SPEAKER_00", "hi")], "SPEAKER_07")
    assert override["id"] == "SPEAKER_07"
    assert override["selection"] == "override"
