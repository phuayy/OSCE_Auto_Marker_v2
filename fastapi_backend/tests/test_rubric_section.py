"""Guards the 'Could not locate rubric section in case-study PDF' failure
(scripts/nvidia_osce_assessor.py:1345).

The detection works for valid case-study PDFs; these tests pin that behaviour,
add a guard for the page-boundary-split header edge case, and assert the new
actionable diagnostics for the real failure modes (wrong PDF / scanned PDF).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "rubric_section.py"
_spec = importlib.util.spec_from_file_location("rubric_section", _SCRIPT)
rubric_section = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rubric_section)

split = rubric_section.split_case_study_context_and_rubric
diagnose = rubric_section.diagnose_missing_rubric_section

_VALID = (
    "[Page 1]\nPatient presents with an itchy rash on the foot.\n\n"
    "[Page 4]\nAnalytical Checklist\nGATHERING INFORMATION/ INTRODUCTION YES NO\n"
    "1. Elicits onset and duration.\n2. Asks about other symptoms.\n"
    "References: PHR1012 notes."
)


def test_valid_case_study_splits_out_rubric() -> None:
    context, rubric = split(_VALID)
    assert "itchy rash" in context
    assert rubric.startswith("Analytical Checklist")
    # The trailing references block is trimmed from the rubric.
    assert "References:" not in rubric


def test_header_split_across_page_boundary_is_matched() -> None:
    """Bug guard: a header split by a page marker must still be located."""
    text = (
        "Context about the patient that is long enough to be real content.\n\n"
        "Analytical\n\n[Page 5]\nChecklist\nGATHERING INFORMATION YES NO\n1. Item."
    )
    _context, rubric = split(text)
    assert rubric  # non-empty -> rubric located despite the page split
    assert "Checklist" in rubric


def test_wrong_pdf_yields_no_rubric_and_actionable_message() -> None:
    # Standalone-rubric / unrelated PDF: real text, but no embedded rubric header.
    wrong = "[Page 1]\n" + ("Pharmacy Practice Module Overview. " * 20) + "\nLearning Outcomes\nAssessment Weighting"
    _context, rubric = split(wrong)
    assert rubric == ""
    message = diagnose(wrong)
    assert "wrong file" in message.lower()
    assert "headings detected" in message.lower()


def test_scanned_image_pdf_is_called_out() -> None:
    scanned = "[Page 1]\n   \n[Page 2]\n  "  # almost no real text after markers
    message = diagnose(scanned)
    assert "scanned" in message.lower() or "image" in message.lower()


def test_partial_rubric_wording_is_distinguished() -> None:
    partial = "[Page 1]\n" + ("Detailed clinical scenario text. " * 15) + "\nChecklist of medications dispensed today."
    assert split(partial)[1] == ""  # no full header -> no rubric
    message = diagnose(partial)
    assert "rubric-like wording" in message.lower()
