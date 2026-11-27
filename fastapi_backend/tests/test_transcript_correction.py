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
    assert len(corrections) == 1
    assert corrections[0]["segmentId"] == 1
    assert corrections[0]["original"] == "nasal blog"
    assert corrections[0]["corrected"] == "nasal block"
    assert corrections[0]["method"] == "orthographic"
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


# -- real clinical words must not be rewritten into a different real term ------

CONFUSABLE_PAIRS = [
    ("hypoglycaemia", "hyperglycaemia"),
    ("dysphasia", "dysphagia"),
    ("prednisone", "prednisolone"),
]


def test_a_correct_clinical_word_is_not_rewritten_into_its_confusable_sibling() -> None:
    for spoken, corpus_term in CONFUSABLE_PAIRS:
        segment = make_segment(f"The patient has {spoken} today.")
        assert correct_segments([segment], [corpus_term]) == [], (spoken, corpus_term)
        assert spoken in segment["text"]


def test_the_confusable_pairs_are_protected_in_both_directions() -> None:
    for left, right in CONFUSABLE_PAIRS:
        for spoken, corpus_term in ((left, right), (right, left)):
            segment = make_segment(f"History of {spoken}.")
            assert correct_segments([segment], [corpus_term]) == [], (spoken, corpus_term)


def test_a_protected_word_inside_a_multi_word_window_is_left_alone() -> None:
    segment = make_segment("Is it hypoglycaemia or something else?")
    assert correct_segments([segment], ["hyperglycaemia", "something else"]) == []
    assert segment["text"] == "Is it hypoglycaemia or something else?"


def test_case_and_punctuation_variants_of_a_lexicon_word_stay_protected() -> None:
    for text in ("Hypoglycaemia, again.", "HYPOGLYCAEMIA!", "(dysphasia)"):
        segment = make_segment(text)
        assert correct_segments([segment], ["hyperglycaemia", "dysphagia"]) == [], text
        assert segment["text"] == text


def test_genuine_misspellings_are_still_corrected() -> None:
    segment = make_segment("He took paracetemol and para set a mole.")
    corrections = correct_segments([segment], ["paracetamol"])

    assert segment["text"] == "He took paracetamol and paracetamol."
    assert len(corrections) == 2


def test_a_misspelling_next_to_a_protected_word_is_still_corrected() -> None:
    segment = make_segment("Hypoglycaemia was treated with paracetemol.")
    correct_segments([segment], ["hyperglycaemia", "paracetamol"])

    assert segment["text"] == "Hypoglycaemia was treated with paracetamol."


def test_subtitle_patching_does_not_touch_a_protected_word(tmp_path: Path) -> None:
    segment = make_segment("Hypoglycaemia and parasitamol.")
    corrections = correct_segments([segment], ["hyperglycaemia", "paracetamol"])
    subtitle = tmp_path / "a.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:02,000\nHypoglycaemia and parasitamol.\n", encoding="utf-8")

    apply_replacements_to_file(subtitle, corrections)

    assert subtitle.read_text(encoding="utf-8").splitlines()[2] == "Hypoglycaemia and paracetamol."
