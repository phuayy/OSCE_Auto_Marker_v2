#!/usr/bin/env python3
"""Parse the PHR1012 communication rubric PDF into structured JSON.

The parser is run server-side whenever the rubric PDF is uploaded/replaced. It
emits a JSON file at storage/auth/communication_rubric.json (or wherever the
caller specifies via --output) that downstream scoring code can consume without
re-parsing the PDF every time.

Expected PDF structure (loose, the parser is defensive):
- Title line "PHR1012 COMMUNICATION RUBRICS"
- Optional section headers (free-form, e.g. "Communicative effectiveness")
- 7 numbered criteria of the form "N. <label>" followed by one or more bullet
  performance indicators starting with "•"
- A "Notes:" block at the end explaining the 0-3 mark scheme

Scoring scheme is fixed in this product:
    All  = 3
    Most = 2
    Some = 1
    None = 0
Maximum points = 3 * number_of_criteria
Pass threshold for the canonical 7-criterion rubric = 11/21. For rubrics with a
different criterion count we fall back to ceil(max_points / 2).
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any


SCORE_LEVELS = {
    "All": 3,
    "Most": 2,
    "Some": 1,
    "None": 0,
}

SECTION_HEADER_PATTERNS = [
    re.compile(r"communicative\s+effectiveness", flags=re.IGNORECASE),
    re.compile(
        r"application\s+of\s+interpersonal\s+communication\s+to\s+address\s+problems?",
        flags=re.IGNORECASE,
    ),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parse the communication rubric PDF into JSON.")
    parser.add_argument("--pdf", required=True, help="Path to the communication rubric PDF.")
    parser.add_argument("--output", help="Where to write parsed JSON (defaults to stdout-only).")
    parser.add_argument(
        "--stdout-only",
        action="store_true",
        help="Print JSON to stdout and skip writing --output.",
    )
    return parser.parse_args()


def read_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as error:
        raise RuntimeError(
            "Missing dependency 'pypdf'. Install it with `pip install pypdf`."
        ) from error

    parts: list[str] = []
    with path.open("rb") as pdf_file:
        reader = PdfReader(pdf_file)
        for page in reader.pages:
            page_text = (page.extract_text() or "").strip()
            if page_text:
                parts.append(page_text)

    if not parts:
        raise RuntimeError(f"No extractable text found in PDF {path.name}.")

    return "\n".join(parts)


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


CANONICAL_SECTION_NAMES = {
    "communicative effectiveness": "Communicative effectiveness",
    "application of interpersonal communication to address problems": (
        "Application of interpersonal communication to address problems"
    ),
    "application of interpersonal communication to address problem": (
        "Application of interpersonal communication to address problems"
    ),
}


def canonicalize_section_name(raw_name: str) -> str:
    cleaned = normalize_whitespace(raw_name).lower()
    if cleaned in CANONICAL_SECTION_NAMES:
        return CANONICAL_SECTION_NAMES[cleaned]
    return normalize_whitespace(raw_name).capitalize() or "Uncategorized"


def find_section_spans(compact_text: str) -> list[dict[str, Any]]:
    """Return ordered list of {name, start, end} spans covering section header
    occurrences inside the compact rubric text. `end` is exclusive."""
    spans: list[dict[str, Any]] = []
    for pattern in SECTION_HEADER_PATTERNS:
        for match in pattern.finditer(compact_text):
            spans.append(
                {
                    "name": canonicalize_section_name(match.group(0)),
                    "start": match.start(),
                    "end": match.end(),
                }
            )
    spans.sort(key=lambda item: item["start"])
    return spans


def section_for_position(spans: list[dict[str, Any]], position: int) -> str | None:
    """Find which section header most recently preceded `position`."""
    current: str | None = None
    for span in spans:
        if span["start"] <= position:
            current = span["name"]
        else:
            break
    return current


def trim_dangling_punctuation(value: str) -> str:
    cleaned = normalize_whitespace(value)
    cleaned = re.sub(r"\s+([.,;:])", r"\1", cleaned)
    cleaned = re.sub(r"([.,;:])\s*$", r"\1", cleaned)
    cleaned = re.sub(r"\.\s*\.+$", ".", cleaned)
    return cleaned.strip(" -")


def parse_rubric_text(raw_text: str) -> dict[str, Any]:
    text = str(raw_text or "")

    # Normalize the entire blob to a single-line string so layout-aware parsers
    # can use simple character offsets to locate criteria and section headers.
    compact = re.sub(r"\s+", " ", text).strip()

    title_match = re.search(r"PHR\d{4}\s+COMMUNICATION\s+RUBRICS?", compact, flags=re.IGNORECASE)
    title = title_match.group(0).strip() if title_match else "Communication Rubric"

    notes_match = re.search(r"\bNotes?\s*:\s*", compact, flags=re.IGNORECASE)
    body = compact[: notes_match.start()] if notes_match else compact
    notes_text = compact[notes_match.end() :].strip() if notes_match else ""

    section_spans = find_section_spans(body)

    # Find every numbered criterion start ("1. ", "2. ", ...). The negative
    # lookbehind keeps it from matching digits embedded in section text.
    criterion_pattern = re.compile(r"(?<!\d)(\d{1,2})\.\s+", flags=re.MULTILINE)
    criterion_matches = list(criterion_pattern.finditer(body))

    if not criterion_matches:
        raise RuntimeError(
            "Could not extract any numbered communication criteria from the PDF. "
            "Verify the PDF has '1.', '2.', ... entries followed by '•' bullet indicators."
        )

    criteria: list[dict[str, Any]] = []
    seen_ids: set[int] = set()

    for index, match in enumerate(criterion_matches):
        criterion_id = int(match.group(1))
        if criterion_id in seen_ids:
            continue
        seen_ids.add(criterion_id)

        body_start = match.end()
        body_end = criterion_matches[index + 1].start() if index + 1 < len(criterion_matches) else len(body)
        chunk = body[body_start:body_end]

        # Bullet indicators are separated by "•". The first segment is the
        # criterion label, the rest are individual indicators.
        chunk_parts = [normalize_whitespace(part) for part in chunk.split("•")]
        chunk_parts = [part for part in chunk_parts if part]
        if not chunk_parts:
            continue

        label = chunk_parts[0]
        indicators = chunk_parts[1:]

        # Strip any section header text that bled into indicators (e.g. an
        # indicator ending with "...next section Application of interpersonal
        # communication...") or the label itself.
        for index, indicator in enumerate(indicators):
            cleaned = indicator
            for pattern in SECTION_HEADER_PATTERNS:
                cleaned = pattern.sub(" ", cleaned)
            indicators[index] = trim_dangling_punctuation(cleaned)
        indicators = [item for item in indicators if item]

        for pattern in SECTION_HEADER_PATTERNS:
            label = pattern.sub(" ", label)
        label = trim_dangling_punctuation(label)

        section_name = section_for_position(section_spans, match.start())

        criteria.append(
            {
                "id": criterion_id,
                "section": section_name,
                "label": label,
                "indicators": indicators,
            }
        )

    if not criteria:
        raise RuntimeError("Failed to construct any criteria from the parsed rubric.")

    criteria.sort(key=lambda item: int(item.get("id", 0)))

    # Build a clean list of sections that actually contain criteria.
    sections: list[dict[str, Any]] = []
    section_to_criteria: dict[str, list[int]] = {}
    section_order: list[str] = []
    for criterion in criteria:
        section_name = criterion.get("section") or "Uncategorized"
        if section_name not in section_to_criteria:
            section_to_criteria[section_name] = []
            section_order.append(section_name)
        section_to_criteria[section_name].append(int(criterion["id"]))

    for section_name in section_order:
        sections.append(
            {
                "name": section_name,
                "criteria_ids": section_to_criteria[section_name],
            }
        )

    criterion_count = len(criteria)
    max_score = criterion_count * 3
    if criterion_count == 7:
        pass_threshold = 11
    else:
        pass_threshold = math.ceil(max_score / 2)

    return {
        "schema": "communication-rubric-v1",
        "title": title,
        "scoring_scale": SCORE_LEVELS,
        "max_score": max_score,
        "pass_threshold": pass_threshold,
        "criteria_count": criterion_count,
        "sections": sections,
        "criteria": criteria,
        "notes": notes_text,
    }


def main() -> int:
    args = parse_args()
    pdf_path = Path(args.pdf).expanduser().resolve()
    if not pdf_path.exists():
        raise FileNotFoundError(f"Rubric PDF does not exist: {pdf_path}")

    raw_text = read_pdf_text(pdf_path)
    payload = parse_rubric_text(raw_text)
    payload["source_pdf"] = str(pdf_path)

    output_text = json.dumps(payload, indent=2, ensure_ascii=False)

    if args.output and not args.stdout_only:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output_text + "\n", encoding="utf-8")
        print(f"Saved: {output_path}", file=sys.stderr)

    print(output_text)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
