"""Phonetic channel of the corpus-term corrector.

Covers what edit distance cannot reach: a transcriber that renders a clinical
term as a different, plausible sequence of ordinary words.
"""
from __future__ import annotations

from app.pipeline.transcript_correction import CorrectionPolicy, correct_segments


CORPUS = [
    "paracetamol",
    "amoxicillin",
    "salbutamol",
    "prednisolone",
    "metformin",
    "nasal block",
    "shortness of breath",
]


def make_segment(text: str, segment_id: int = 1) -> dict:
    return {"id": segment_id, "text": text}


def test_word_split_across_tokens_is_recovered() -> None:
    segment = make_segment("I took para set a mole for the fever.")
    corrections = correct_segments([segment], CORPUS)

    assert segment["text"] == "I took paracetamol for the fever."
    assert corrections[0]["original"] == "para set a mole"
    assert corrections[0]["corrected"] == "paracetamol"
    assert corrections[0]["method"] == "phonetic"
    assert corrections[0]["phoneticRatio"] == 1.0


def test_near_miss_phonetics_are_accepted() -> None:
    segment = make_segment("She uses sal butter mole twice a day.")
    corrections = correct_segments([segment], CORPUS)

    assert segment["text"] == "She uses salbutamol twice a day."
    assert corrections[0]["method"] == "phonetic"
    assert 0.90 <= corrections[0]["phoneticRatio"] < 1.0


def test_punctuation_and_capitalisation_survive_a_span_replacement() -> None:
    segment = make_segment("Met form in, twice daily.")
    corrections = correct_segments([segment], CORPUS)

    assert segment["text"] == "Metformin, twice daily."
    assert corrections[0]["corrected"] == "Metformin"


def test_multi_word_term_recovered_from_a_longer_span() -> None:
    segment = make_segment("He complained of short nurse of breath on the stairs.")
    corrections = correct_segments([segment], CORPUS)

    assert segment["text"] == "He complained of shortness of breath on the stairs."
    assert corrections[0]["original"] == "short nurse of breath"


def test_sibling_drug_already_spelled_correctly_is_not_rewritten() -> None:
    # Both are corpus terms and their keys are 0.92 similar; a correct
    # transcription must never be "corrected" into its sibling.
    segment = make_segment("The patient takes prednisolone every morning.")
    corpus = [*CORPUS, "prednisone"]

    assert correct_segments([segment], corpus) == []
    assert segment["text"] == "The patient takes prednisolone every morning."


def test_ordinary_speech_is_left_alone() -> None:
    text = (
        "Good morning, I am a third year medical student. "
        "Can you tell me what brought you in to see us today?"
    )
    segment = make_segment(text)

    assert correct_segments([segment], CORPUS) == []
    assert segment["text"] == text


def test_short_similar_sounding_words_are_not_swapped() -> None:
    segment = make_segment("The patient wore a black jacket and sat on a mole hill.")
    corrections = correct_segments([segment], ["block", "mold", *CORPUS])

    assert corrections == []


def test_phonetic_channel_can_be_disabled() -> None:
    segment = make_segment("I took para set a mole for the fever.")
    policy = CorrectionPolicy(phonetic_enabled=False)

    assert correct_segments([segment], CORPUS, policy) == []
    assert segment["text"] == "I took para set a mole for the fever."


def test_orthographic_channel_still_reports_its_own_method() -> None:
    segment = make_segment("I took parasitamol for the fever.")
    corrections = correct_segments([segment], CORPUS)

    assert segment["text"] == "I took paracetamol for the fever."
    assert corrections[0]["method"] == "orthographic"


def test_span_limit_is_enforced() -> None:
    # Two extra words allowed, so the four-token rendering is out of reach.
    policy = CorrectionPolicy(max_extra_span_words=2)
    segment = make_segment("I took para set a mole for the fever.")

    assert correct_segments([segment], CORPUS, policy) == []


def test_longest_term_wins_and_spans_stay_locked() -> None:
    segment = make_segment("Any nay zal blog or para set a mole use?")
    corrections = correct_segments([segment], CORPUS)

    assert segment["text"] == "Any nasal block or paracetamol use?"
    assert {correction["corrected"] for correction in corrections} == {"nasal block", "paracetamol"}


def test_multiple_segments_are_logged_against_their_own_ids() -> None:
    segments = [
        make_segment("A moxie sillin three times a day.", 4),
        make_segment("No allergies that I know of.", 5),
    ]
    corrections = correct_segments(segments, CORPUS)

    assert segments[0]["text"] == "Amoxicillin three times a day."
    assert [correction["segmentId"] for correction in corrections] == [4]


def test_common_phrase_is_not_pulled_into_a_multi_word_term() -> None:
    # Regression: at the relaxed threshold "sure that" scored 0.80 against
    # "sore throat", which rewrote "make sure that everything" into nonsense.
    segment = make_segment("Just to make sure that everything is appropriate for you.")
    corrections = correct_segments([segment], ["sore throat", *CORPUS])

    assert corrections == []
    assert segment["text"] == "Just to make sure that everything is appropriate for you."
