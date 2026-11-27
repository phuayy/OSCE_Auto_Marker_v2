"""Criterion numbering counts only as list structure, never as a number in prose."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import case_study_rubric  # noqa: E402
from content_marking import extract_rubric_criteria_from_case_study_rubric as extract  # noqa: E402


def labels(text: str) -> list[str]:
    return [item["label"] for item in extract(text)]


def test_real_numbered_list_is_unchanged():
    text = (
        "Analytical Checklist\nGATHERING INFORMATION YES NO\n"
        "1. Elicits onset and duration.\n"
        "2. [CRITICAL CRITERIA] Asks about allergies\n"
        "3. Advises the patient.\nCut score 2 of 3\n"
    )
    assert extract(text) == [
        {"label": "Elicits onset and duration", "is_critical": False},
        {"label": "Asks about allergies", "is_critical": True},
        {"label": "Advises the patient", "is_critical": False},
    ]


def test_age_inside_a_criterion_does_not_split_it():
    text = (
        "1. Confirms the patient is aged 65. Then asks about falls.\n"
        "2. Checks medicines.\n"
        "3. Gives advice.\n"
    )
    assert labels(text) == [
        "Confirms the patient is aged 65. Then asks about falls",
        "Checks medicines",
        "Gives advice",
    ]


def test_age_wrapped_to_a_line_start_is_ignored_because_it_is_out_of_sequence():
    text = (
        "1. Confirms the patient is aged\n65. Then asks about falls.\n"
        "2. Checks medicines.\n"
    )
    assert labels(text) == ["Confirms the patient is aged 65. Then asks about falls", "Checks medicines"]


def test_numbers_out_of_sequence_and_mid_line_are_ignored():
    text = "1. First thing, see section 3. Then more.\n2. Second thing.\n7. stray\n3. Third thing.\n"
    assert labels(text) == ["First thing, see section 3. Then more", "Second thing. 7. stray", "Third thing"]


def test_decimal_at_line_start_is_not_a_marker():
    text = "1. Counsels on dose.\n2.5 mg twice daily.\n2. Gives advice.\n"
    assert labels(text) == ["Counsels on dose. 2.5 mg twice daily", "Gives advice"]


def test_repeated_numbering_keeps_the_first_list():
    text = "1. A thing.\n2. B thing.\n1. A again.\n"
    assert labels(text) == ["A thing", "B thing. 1. A again"]


def test_empty_input():
    assert extract("") == []
    assert extract("no list here, aged 65. ") == []


def test_extractor_version_was_bumped_so_cached_rubrics_are_re_extracted():
    assert case_study_rubric.EXTRACTOR_VERSION != "case-study-rubric-v1"


@pytest.mark.parametrize("text", ["1. Only one.\n"])
def test_single_criterion(text):
    assert labels(text) == ["Only one"]
