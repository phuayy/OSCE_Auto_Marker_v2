from __future__ import annotations

from app.pipeline.hallucination_filter import (
    FILLER_PHRASES,
    compression_ratio,
    max_ngram_repeats,
    normalize_phrase,
    screen_segments,
)


def make_segment(text: str, segment_id: int = 1, **extra) -> dict:
    return {"id": segment_id, "start": 0.0, "end": 1.0, "speaker": "SPEAKER_00", "text": text, **extra}


def test_clean_speech_is_not_flagged() -> None:
    segments = [
        make_segment("Good morning, my name is Alex and I am a third year medical student.", 1),
        make_segment("Can you tell me more about the chest pain you mentioned?", 2, avgLogprob=-0.32),
    ]
    kept, flags = screen_segments(segments)

    assert flags == []
    assert kept == segments
    assert "hallucination" not in segments[0]


def test_low_avg_logprob_is_flagged() -> None:
    segment = make_segment("And then the appointment was arranged for next week.", avgLogprob=-1.4)
    _, flags = screen_segments([segment])

    assert flags[0]["reasons"] == ["low_avg_logprob"]
    assert flags[0]["metrics"]["avgLogprob"] == -1.4
    assert segment["hallucination"]["action"] == "flagged"


def test_avg_logprob_at_threshold_is_kept() -> None:
    segment = make_segment("The pain started three days ago after lunch.", avgLogprob=-1.0)
    _, flags = screen_segments([segment])

    assert flags == []


def test_missing_avg_logprob_is_screened_on_text_alone() -> None:
    # Canary-Qwen reports no per-segment confidence.
    segment = make_segment("Any allergies to medication that you know of?")
    _, flags = screen_segments([segment])

    assert flags == []


def test_repetition_trips_compression_ratio_and_ngram() -> None:
    segment = make_segment("I understand. " * 8)
    _, flags = screen_segments([segment])

    assert "high_compression_ratio" in flags[0]["reasons"]
    assert "repeated_ngram" in flags[0]["reasons"]
    assert flags[0]["metrics"]["repeatedNgram"] == "i understand"


def test_ngram_repeated_exactly_twice_is_kept() -> None:
    segment = make_segment("Okay okay, so when did the cough start?")
    _, flags = screen_segments([segment])

    assert flags == []


def test_filler_phrase_is_flagged() -> None:
    segment = make_segment(" Thank you for watching!")
    _, flags = screen_segments([segment])

    assert flags[0]["reasons"] == ["filler_phrase"]


def test_filler_substring_inside_real_speech_survives() -> None:
    segment = make_segment("Thank you for coming in today, please take a seat.")
    _, flags = screen_segments([segment])

    assert flags == []


def test_drop_removes_flagged_segments_and_keeps_ids_stable() -> None:
    segments = [
        make_segment("What brings you in today?", 1),
        make_segment("Subtitles by the Amara.org community", 2),
        make_segment("Does the pain radiate anywhere?", 3),
    ]
    kept, flags = screen_segments(segments, drop=True)

    assert [segment["id"] for segment in kept] == [1, 3]
    assert flags[0]["segmentId"] == 2
    assert flags[0]["action"] == "dropped"


def test_drop_is_downgraded_when_every_segment_is_flagged() -> None:
    segments = [make_segment("Thank you.", 1), make_segment("Bye.", 2)]
    kept, flags = screen_segments(segments, drop=True)

    assert len(kept) == 2
    assert all(flag["action"] == "flagged" for flag in flags)
    assert all(flag["retained"] == "every_segment_flagged" for flag in flags)


def test_flag_record_is_explainable() -> None:
    segment = make_segment("No no no no no no.", 7, avgLogprob=-1.8)
    _, flags = screen_segments([segment])
    record = flags[0]

    assert record["segmentId"] == 7
    assert record["text"] == "No no no no no no."
    assert record["speaker"] == "SPEAKER_00"
    assert set(record["reasons"]) >= {"low_avg_logprob", "repeated_ngram"}
    assert record["metrics"]["ngramRepeats"] == 6


def test_filler_set_is_stored_in_normalized_form() -> None:
    # A phrase carrying punctuation ("Amara.org") would never match, because
    # lookup happens on the normalized text.
    assert all(phrase == normalize_phrase(phrase) for phrase in FILLER_PHRASES)


def test_helpers() -> None:
    assert normalize_phrase("  Thank YOU!! ") == "thank you"
    assert compression_ratio("") == 0.0
    assert max_ngram_repeats("") == (0, "")
    assert max_ngram_repeats("ah ah ah") == (3, "ah")
