"""Locate and extract the embedded rubric ('Analytical Checklist') section from
a case-study PDF's text.

Dependency-free (stdlib ``re`` only) so it is importable both by the scorer
subprocess (``nvidia_osce_assessor.py``) and by unit tests under any interpreter.
"""
from __future__ import annotations

import re

# Ordered regex markers that identify the start of the embedded rubric section.
# The first two are exact (fast path); the third tolerates the header being
# split across a page boundary (e.g. "Analytical\n\n[Page 5]\nChecklist"), which
# the prior exact-only matcher silently missed.
RUBRIC_SECTION_MARKERS: tuple[str, ...] = (
    r"analytical\s+checklist",
    r"gathering\s+information\s*/\s*introduction\s+yes\s+no",
    r"analytical(?:\s|\[page[^\]]*\])+checklist",
)

# A case-study PDF that yields fewer than this many characters of real text
# (after stripping the synthetic "[Page N]" markers) is treated as scanned/image
# only — extraction, not the rubric, is the problem.
MIN_EXTRACTABLE_TEXT_CHARS = 200

_PAGE_MARKER_RE = re.compile(r"\[page\s*\d+\]", flags=re.IGNORECASE)
_REFERENCES_RE = re.compile(r"\breferences?\s*:", flags=re.IGNORECASE)


def split_case_study_context_and_rubric(case_study_text: str) -> tuple[str, str]:
    """Split case-study text into (clinical context, rubric section).

    Returns ``(full_text, "")`` when no rubric marker is found, so callers can
    detect the missing-rubric condition and surface an actionable error.
    """
    full_text = str(case_study_text or "").strip()
    if not full_text:
        return "", ""

    rubric_start_index = -1
    for pattern in RUBRIC_SECTION_MARKERS:
        marker_match = re.search(pattern, full_text, flags=re.IGNORECASE)
        if marker_match:
            rubric_start_index = marker_match.start()
            break

    if rubric_start_index < 0:
        return full_text, ""

    case_context = full_text[:rubric_start_index].strip()
    rubric_section = full_text[rubric_start_index:].strip()

    references_match = _REFERENCES_RE.search(rubric_section)
    if references_match:
        rubric_section = rubric_section[: references_match.start()].strip()

    return case_context, rubric_section


def diagnose_missing_rubric_section(case_study_text: str) -> str:
    """Return an actionable explanation for why the rubric could not be located.

    Distinguishes the three real-world causes so the operator knows what to do:
    a scanned/image PDF, a wrong/unrelated PDF, or a present-but-unparseable
    rubric header.
    """
    full_text = str(case_study_text or "")
    body = _PAGE_MARKER_RE.sub(" ", full_text).strip()

    if len(body) < MIN_EXTRACTABLE_TEXT_CHARS:
        return (
            "The case-study PDF yielded almost no extractable text "
            f"({len(body)} chars), so it is most likely a scanned/image-only PDF. "
            "Re-export it as a text-based PDF (or run OCR) before scoring."
        )

    lower = body.lower()
    if "analytical" in lower or "checklist" in lower:
        return (
            "The case-study PDF contains rubric-like wording but the "
            "'Analytical Checklist' section header could not be located "
            "(it may be reordered, reformatted, or split by the PDF layout). "
            "Confirm the rubric table is intact at the end of the document."
        )

    headings = [
        line.strip()
        for line in body.splitlines()
        if 3 <= len(line.strip()) <= 60 and line.strip()[:1].isupper()
    ]
    sample = "; ".join(headings[-8:]) or "none detected"
    return (
        "No embedded rubric ('Analytical Checklist') section was found. The "
        "case-study PDF must contain the rubric at the end; you may have uploaded "
        "the wrong file (e.g. the standalone rubric PDF or an unrelated document). "
        f"Section headings detected near the end: {sample}."
    )
