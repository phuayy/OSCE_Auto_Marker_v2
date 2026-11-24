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
import re
import sys
import os
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import llm_bootstrap  # noqa: E402,F401  (puts fastapi_backend on sys.path)
from app.domain.communication_rubric import (  # noqa: E402
    CANONICAL_SECTION_NAMES,
    build_rubric_payload,
    canonicalize_section_name,
    normalize_whitespace,
    rubric_quality_issues,
)
from rubric_llm_fallback import (  # noqa: E402
    RubricLlmFallbackFailed,
    RubricLlmFallbackUnavailable,
    deterministic_provenance,
    extract_rubric_with_llm,
    fallback_enabled,
)

__all__ = [
    "CANONICAL_SECTION_NAMES",
    "build_payload",
    "build_rubric_payload",
    "canonicalize_section_name",
    "normalize_whitespace",
    "rubric_quality_issues",
]


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
    parser.add_argument(
        "--no-llm-fallback",
        action="store_true",
        help="Never fall back to LLM extraction when the deterministic parser fails.",
    )
    parser.add_argument(
        "--force-llm",
        action="store_true",
        help="Skip the deterministic parser and extract with the LLM only.",
    )
    args = parser.parse_args()
    if args.force_llm and args.no_llm_fallback:
        parser.error("--force-llm cannot be combined with --no-llm-fallback.")
    return args


def read_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as error:
        raise RuntimeError(
            "Missing dependency 'pypdf'. Install it with `uv sync`."
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

    return build_rubric_payload(title=title, criteria=criteria, notes=notes_text)


def payload_from_llm(extraction_payload: dict[str, Any]) -> dict[str, Any]:
    """Build the rubric payload from an LLM extraction via the shared builder."""
    criteria: list[dict[str, Any]] = []
    for index, item in enumerate(extraction_payload.get("criteria") or [], start=1):
        raw_section = normalize_whitespace(item.get("section") or "")
        criteria.append(
            {
                "id": index,
                "section": canonicalize_section_name(raw_section) if raw_section else None,
                "label": normalize_whitespace(item.get("label", "")),
                "indicators": [
                    text
                    for text in (normalize_whitespace(i) for i in item.get("indicators") or [])
                    if text
                ],
            }
        )
    return build_rubric_payload(
        title=normalize_whitespace(extraction_payload.get("title") or "") or "Communication Rubric",
        criteria=criteria,
        notes=normalize_whitespace(extraction_payload.get("notes") or ""),
    )


def _build_payload_forced_llm(raw_text: str) -> dict[str, Any]:
    """LLM-only extraction, for an admin who explicitly asked for it."""
    if not fallback_enabled():
        raise RuntimeError(
            "AI extraction is turned off on this server (RUBRIC_LLM_FALLBACK=false)."
        )
    try:
        extraction = extract_rubric_with_llm("communication", raw_text)
    except RubricLlmFallbackUnavailable as error:
        raise RuntimeError(f"AI extraction is not available: {error}") from error
    except RubricLlmFallbackFailed as error:
        raise RuntimeError(f"AI extraction failed: {error}") from error
    payload = payload_from_llm(extraction.payload)
    # Nothing failed: the deterministic parser was never asked.
    payload["extraction"] = extraction.provenance(deterministic_error=None)
    return payload


def build_payload(
    raw_text: str, *, allow_llm: bool = True, force_llm: bool = False
) -> dict[str, Any]:
    """Deterministic parse first; LLM extraction only when it fails the gate.

    ``force_llm`` skips the deterministic parser and uses the LLM alone.
    """
    if force_llm:
        return _build_payload_forced_llm(raw_text)
    deterministic_error: str | None = None
    payload: dict[str, Any] | None = None
    try:
        payload = parse_rubric_text(raw_text)
        issues = rubric_quality_issues(payload)
        if issues:
            deterministic_error = "; ".join(issues)
    except Exception as error:  # noqa: BLE001 - any parser failure triggers the fallback
        deterministic_error = str(error)

    if deterministic_error is None and payload is not None:
        payload["extraction"] = deterministic_provenance()
        return payload

    if not allow_llm:
        if payload is None:
            raise RuntimeError(deterministic_error)
        payload["extraction"] = deterministic_provenance()
        return payload

    try:
        extraction = extract_rubric_with_llm("communication", raw_text)
    except RubricLlmFallbackUnavailable as error:
        if payload is None:
            # Unchanged behaviour: the deterministic parser's own message.
            raise RuntimeError(deterministic_error) from error
        print(
            f"Warning: rubric looks incomplete ({deterministic_error}) and the LLM fallback "
            f"is unavailable ({error}); keeping the deterministic parse.",
            file=sys.stderr,
        )
        payload["extraction"] = deterministic_provenance()
        return payload
    except RubricLlmFallbackFailed as error:
        raise RuntimeError(
            f"Deterministic parser: {deterministic_error}. LLM extraction fallback: {error}"
        ) from error

    llm_payload = payload_from_llm(extraction.payload)
    llm_payload["extraction"] = extraction.provenance(deterministic_error=deterministic_error)
    return llm_payload


def main() -> int:
    args = parse_args()
    pdf_path = Path(args.pdf).expanduser().resolve()
    if not pdf_path.exists():
        raise FileNotFoundError(f"Rubric PDF does not exist: {pdf_path}")

    # By-hand runs only: a real run already receives its environment from the
    # server. Restricted to what the fallback's router reads.
    from env_loader import load_env_file
    from scorer_env_allowlist import RUBRIC_PARSER_ENV_ALLOWLIST

    load_env_file(ROOT_DIR, allowed_keys=RUBRIC_PARSER_ENV_ALLOWLIST)

    # A PDF with no text at all (scanned) raises here, before any LLM call.
    raw_text = read_pdf_text(pdf_path)
    payload = build_payload(
        raw_text, allow_llm=not args.no_llm_fallback, force_llm=args.force_llm
    )
    payload["source_pdf"] = str(pdf_path)
    print(f"Rubric extraction method: {payload['extraction']['method']}", file=sys.stderr)

    output_text = json.dumps(payload, indent=2, ensure_ascii=False)

    if args.output and not args.stdout_only:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
        try:
            temp_path.write_text(output_text + "\n", encoding="utf-8")
            os.replace(temp_path, output_path)
        finally:
            temp_path.unlink(missing_ok=True)
        print(f"Saved: {output_path}", file=sys.stderr)

    print(output_text)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
