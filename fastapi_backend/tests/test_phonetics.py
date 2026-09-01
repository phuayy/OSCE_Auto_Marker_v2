"""Behavioural contract of the vendored Double Metaphone encoder.

The assertions pin the properties the transcript corrector depends on — which
spellings must collide and which must not — plus a handful of canonical outputs
from the published algorithm.
"""
from __future__ import annotations

import pytest

from app.pipeline.phonetics import double_metaphone, phonetic_codes, phonetic_key


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("Smith", ("SM0", "XMT")),
        ("Schmidt", ("XMT", "SMT")),
        ("Thompson", ("TMPSN", "TMPSN")),
        ("Wright", ("RT", "RT")),
        ("Knight", ("NT", "NT")),
        ("Xavier", ("SF", "SFR")),
    ],
)
def test_canonical_codes(word: str, expected: tuple[str, str]) -> None:
    assert double_metaphone(word) == expected


@pytest.mark.parametrize(
    ("term", "misrecognition"),
    [
        ("paracetamol", "para set a mole"),
        ("amoxicillin", "a moxie sillin"),
        ("metformin", "met form in"),
        ("prednisolone", "pred nissa loan"),
        ("omeprazole", "omma prazole"),
        ("phenytoin", "fenny toe in"),
        ("ibuprofen", "I be proven"),
        ("atorvastatin", "a torva statin"),
        ("nasal block", "nay zal blog"),
    ],
)
def test_fragmented_misrecognitions_share_the_key(term: str, misrecognition: str) -> None:
    # The whole point: word boundaries are ignored, so a term the transcriber
    # split across several words still encodes to the term's key.
    assert phonetic_key(term) == phonetic_key(misrecognition)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("paracetamol", "prednisolone"),
        ("metformin", "metronidazole"),
        ("warfarin", "verapamil"),
        ("salbutamol", "simvastatin"),
    ],
)
def test_distinct_drugs_do_not_share_the_key(left: str, right: str) -> None:
    assert phonetic_key(left) != phonetic_key(right)


def test_spelling_variants_agree() -> None:
    assert phonetic_key("amoxicillin") == phonetic_key("amoxycillin")
    assert phonetic_key("Smyth") == phonetic_key("Smith")


def test_short_words_collide_and_are_therefore_gated_by_key_length() -> None:
    # "black"/"block" both encode PLK; the corrector's min_phonetic_key_codes
    # guard exists precisely because of pairs like this.
    assert phonetic_key("black") == phonetic_key("block") == "PLK"
    assert len("PLK") < 4


def test_empty_and_symbol_only_input() -> None:
    assert double_metaphone("") == ("", "")
    assert double_metaphone("...") == ("", "")
    assert phonetic_codes(None) == ("", "")  # type: ignore[arg-type]


def test_max_length_restores_reference_truncation() -> None:
    assert double_metaphone("paracetamol") == ("PRSTML", "PRSTML")
    assert double_metaphone("paracetamol", 4) == ("PRST", "PRST")
