from __future__ import annotations

from pathlib import Path

from app.pipeline.transcript_correction import apply_replacements_to_file, correct_segments


TERMS = ["nasal block", "paracetamol", "runny nose", "sore throat"]


def make_segment(text: str, segment_id: int = 1) -> dict:
    return {"id": segment_id, "text": text}


def test_multi_word_near_miss_is_corrected() -> None:
    segment = make_segment("I have a nasal blog since yesterday.")
    corrections = correct_segments([segment], TERMS)

    assert segment["text"] == "I have a nasal block since yesterday."
    assert corrections == [
        {"segmentId": 1, "original": "nasal blog", "corrected": "nasal block", "ratio": corrections[0]["ratio"]}
    ]
    assert corrections[0]["ratio"] >= 0.84


def test_long_single_word_uses_relaxed_threshold() -> None:
    segment = make_segment("I took parasitamol for the fever.")
    corrections = correct_segments([segment], TERMS)

    assert segment["text"] == "I took paracetamol for the fever."
    assert len(corrections) == 1


def test_clean_text_has_no_false_positives() -> None:
    segment = make_segment("The patient wore a black jacket and spoke clearly.")
    corrections = correct_segments([segment], ["block", *TERMS])

    assert corrections == []
    assert segment["text"] == "The patient wore a black jacket and spoke clearly."


def test_exact_matches_are_not_rewritten() -> None:
    segment = make_segment("Any nasal block or runny nose?")
    assert correct_segments([segment], TERMS) == []


def test_punctuation_and_capitalisation_preserved() -> None:
    segment = make_segment("Parasitamol, twice daily.")
    corrections = correct_segments([segment], TERMS)

    assert segment["text"] == "Paracetamol, twice daily."
    assert corrections[0]["original"] == "Parasitamol"
    assert corrections[0]["corrected"] == "Paracetamol"


def test_short_terms_are_ignored() -> None:
    segment = make_segment("He felt il yesterday.")
    assert correct_segments([segment], ["ill"]) == []


def test_apply_replacements_to_subtitle_file(tmp_path: Path) -> None:
    srt_path = tmp_path / "clip.srt"
    srt_path.write_text("1\n00:00:01,000 --> 00:00:02,000\nI have a nasal blog today\n", encoding="utf-8")
    corrections = [{"segmentId": 1, "original": "nasal blog", "corrected": "nasal block", "ratio": 0.857}]

    assert apply_replacements_to_file(srt_path, corrections) is True
    assert "nasal block" in srt_path.read_text(encoding="utf-8")
    # Second application is a no-op (already corrected).
    assert apply_replacements_to_file(srt_path, corrections) is False
    assert apply_replacements_to_file(tmp_path / "missing.srt", corrections) is False
