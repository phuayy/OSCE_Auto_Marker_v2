"""Content marking: the prompt, the rubric extraction and the sheet validator.

Everything a content marker needs *except* the model call lives here, so that
every process which marks — the single-model assessor today, each marker of a
multi-model panel and the panel's adjudicator later — builds the same prompt
from the same inputs and validates the reply against the same rules. Two
markers that must be given "the same prompt and the same transcript" get that
by construction rather than by two scripts staying in step.

``PROMPT_VERSION`` is stamped on every sheet. Bump it whenever the wording of
either prompt changes: sheets produced under different prompt versions are not
comparable, and the agreement statistics a panel reports assume they are.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from llm_bootstrap import normalize_timestamp, read_generic_text, read_json_transcript_text

ROOT_DIR = Path(__file__).resolve().parents[1]

# Bump on any change to build_system_prompt / build_user_prompt wording.
PROMPT_VERSION = "content-marking-v1"

MAX_CASE_STUDY_CONTEXT_CHARS = 36_000
MAX_RUBRIC_SECTION_CHARS = 48_000
MAX_TRANSCRIPT_CHARS = 60_000
# Empty/very short content (below this many characters) is treated as a truncated
# response and triggers a retry instead of falling through to "default everything
# to No / no evidence" in the validator.
MIN_VALID_CONTENT_CHARS = 40


def read_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as error:
        raise RuntimeError(
            "Missing dependency 'pypdf'. Install it with `uv sync` to score from case-study PDFs."
        ) from error

    parts: list[str] = []
    try:
        with path.open("rb") as pdf_file:
            reader = PdfReader(pdf_file)
            for page_index, page in enumerate(reader.pages, start=1):
                page_text = (page.extract_text() or "").strip()
                if page_text:
                    parts.append(f"[Page {page_index}]\n{page_text}")
    except Exception as error:  # pragma: no cover - defensive branch
        raise RuntimeError(f"Failed to open PDF {path.name}: {error}") from error

    if not parts:
        raise RuntimeError(f"No extractable text found in PDF {path.name}.")

    return "\n\n".join(parts)


SRT_TIMESTAMP_PATTERN = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2}[,\.]\d{3})"
)


def read_srt_transcript_text(path: Path) -> str:
    raw_text = read_generic_text(path)
    lines = raw_text.splitlines()

    segments: list[str] = []
    current_start: str | None = None
    current_end: str | None = None
    text_lines: list[str] = []

    def flush_segment() -> None:
        if current_start and current_end and text_lines:
            joined_text = " ".join(text_lines).strip()
            if joined_text:
                segments.append(f"{current_start} - {current_end} | {joined_text}")

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush_segment()
            current_start = None
            current_end = None
            text_lines = []
            continue

        if stripped.upper() == "WEBVTT":
            continue

        timestamp_match = SRT_TIMESTAMP_PATTERN.search(stripped)
        if timestamp_match:
            current_start = timestamp_match.group("start").replace(",", ".")
            current_end = timestamp_match.group("end").replace(",", ".")
            continue

        if stripped.isdigit() and current_start is None and not text_lines:
            continue

        text_lines.append(stripped)

    flush_segment()

    if segments:
        return "\n".join(segments)

    return raw_text


def read_file_as_context_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return read_pdf_text(path)
    if suffix in {".srt", ".vtt"}:
        return read_srt_transcript_text(path)
    if suffix == ".json":
        return read_json_transcript_text(path)
    return read_generic_text(path)


def normalize_is_critical(value: Any) -> bool:
    if isinstance(value, bool):
        return value

    token = str(value or "").strip().lower()
    if token in {"true", "yes", "y", "critical", "1"}:
        return True
    if token in {"false", "no", "n", "non-critical", "0"}:
        return False

    return False


def clean_rubric_label(text: str) -> str:
    cleaned = str(text or "")
    cleaned = re.sub(r"\[\s*critical\s+criteria\s*\]", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"\b(GATHERING INFORMATION\s*/\s*INTRODUCTION|MONITORING\s*/\s*FOLLOW-?UP)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\bOPTION\s*/\s*MANAGEMENT STRATEGIES(?:\s*\([^)]*\)?)?",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\bYES\s+NO\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:;,.\t\n")
    return cleaned


def extract_rubric_criteria_from_case_study_rubric(rubric_section: str) -> list[dict[str, Any]]:
    compact = " ".join(str(rubric_section or "").split())
    if not compact:
        return []

    criterion_pattern = re.compile(
        r"(?<!\d)(\d{1,2})\.\s*(.*?)(?=(?<!\d)\d{1,2}\.\s|cut\s+score\b|references?\s*:|$)",
        flags=re.IGNORECASE,
    )

    parsed: list[tuple[int, dict[str, Any]]] = []
    seen_numbers: set[int] = set()

    for match in criterion_pattern.finditer(compact):
        number = int(match.group(1))
        if number in seen_numbers:
            continue

        raw_label = str(match.group(2) or "").strip()
        if not raw_label:
            continue

        is_critical = bool(
            re.search(r"\[\s*critical\s+criteria\s*\]|\bcritical\s+criteria\b", raw_label, flags=re.IGNORECASE)
        )
        label = clean_rubric_label(raw_label)
        if not label:
            continue

        seen_numbers.add(number)
        parsed.append(
            (
                number,
                {
                    "label": label,
                    "is_critical": is_critical,
                },
            )
        )

    parsed.sort(key=lambda item: item[0])
    return [entry for _number, entry in parsed]


def build_system_prompt(rubric_criteria: list[dict[str, Any]]) -> str:
    rubric_criteria_text = json.dumps(rubric_criteria, ensure_ascii=False, indent=2)

    rubric_requirement = (
        "Use exactly the rubric criteria list below, preserve order, preserve critical flags, and output exactly one item per criterion."
        if rubric_criteria
        else "Infer criteria and critical criteria from the rubric section at the end of the CASE STUDY content only."
    )

    return f"""
You are an OSCE assessment model evaluating a doctor/student interaction with an actor-patient.

CRITICAL reliability context (read carefully):
- The transcript and speaker diarization were produced by an automated speech-to-text
  model. There WILL be transcription errors, incorrect speaker tags, dropped words,
  misheard medical/drug names (e.g. \"clotrimazole\" turning into \"clarithrolaceae\"),
  garbled numbers, and ambiguous turn boundaries.
- Interpret garbled lines charitably. If a word sounds like a plausible misheard
  version of a clinically-relevant term that fits the context, treat it as if the
  student said the correct term. Do NOT penalise the student for transcription
  artefacts. Be CONSISTENT across criteria when judging the same evidence.

Leniency policy (very important — markers are generally lenient):
- Real human OSCE markers grade leniently. When the evidence is borderline, lean
  toward \"Yes\".
- You only need PARTIAL or INDIRECT evidence to award \"Yes\" — e.g. an implied
  acknowledgment, an indirect question, a paraphrased equivalent, or a fragment of
  the expected behaviour is enough. Whole-sentence verbatim is NOT required.
- Default to \"Yes\" unless there is clear, multi-turn evidence the student did
  NOT address the criterion at all. Absence of explicit phrasing is not the same
  as absence of behaviour — if the conversation reasonably implies it, give it.
- For critical criteria, still apply leniency: indirect or partial evidence still
  counts as \"Yes\".

Scoring behavior:
- Score strictly against the rubric section extracted from the end of the case-study PDF.
- Use CASE STUDY CONTEXT for clinical interpretation, but use RUBRIC SECTION for scoring criteria and critical flags.
- In the rubric section, only score criteria that are explicitly Yes/No markable.
- Ignore non-scorable fields (notes, comments, totals, metadata, signatures, free-text admin fields).
- Use only values \"Yes\" or \"No\" for each criterion.
- Each criterion MUST include a timestamp from the transcript (format HH:MM:SS) pointing to the evidence moment.
- If evidence is ambiguous due to transcript quality, lean \"Yes\" and cite the closest moment.
- {rubric_requirement}

You must return ONLY valid JSON, with no markdown and no additional text.

Required JSON shape:
{{
  "session_id": "string",
  "transcript_file": "string",
  "case_study_file": "string",
    "rubric_file": "embedded_in_case_study_pdf",
  "criteria": [
    {{
      "label": "full criterion label",
            "is_critical": true,
      "value": "Yes",
            "timestamp": "HH:MM:SS",
      "reason": "one or more evidence-based sentences"
    }}
  ],
  "keep_start_stop": {{
        "keep": "1-3 coaching sentences for what the student should continue doing",
        "start": "1-3 coaching sentences for what the student should start doing",
        "stop": "1-3 coaching sentences for what the student should stop doing"
  }},
    "overall_summary": "2-4 concise sentences"
}}

KEEP/START/STOP requirements:
- Feedback must be directed to the STUDENT'S performance.
- Feedback must reflect rubric criteria performance.
- Do NOT quote transcript lines.
- Do NOT include speaker tags (e.g., SPEAKER_01) or timestamps.

Rubric criteria to use when available:
{rubric_criteria_text}
""".strip()


def build_user_prompt(
    session_id: str,
    transcript_path: Path,
    case_study_path: Path,
    rubric_criteria: list[dict[str, Any]],
    transcript_text: str,
    case_study_context_text: str,
    rubric_section_text: str,
) -> str:
    rubric_criteria_block = (
        json.dumps(rubric_criteria, ensure_ascii=False, indent=2)
        if rubric_criteria
        else "[No structured extraction available: infer from rubric section only.]"
    )

    return f"""
Please assess this OSCE consultation and return the required JSON only.

Session metadata:
- session_id: {session_id}
- transcript_file: {transcript_path}
- case_study_file: {case_study_path}
- rubric_file: embedded_in_case_study_pdf
- transcript_folder_matches_file_stem: {str(transcript_path.parent.name == transcript_path.stem).lower()}

Scoring source of truth:
- Use only the RUBRIC SECTION (copied below from the end of the case-study PDF) for criteria and critical criteria.
- Do not use any external rubric file.
- Include only criteria that are marked Yes/No in that rubric section.
- Preserve the parsed criteria list order and critical flags exactly when provided.
- Provide a "timestamp" for every criterion using the transcript timestamps (format HH:MM:SS).

Parsed rubric criteria (if available):
---
{rubric_criteria_block}
---

Case study context (non-rubric):
---
{case_study_context_text}
---

Rubric section from end of case study PDF:
---
{rubric_section_text}
---

Transcript content (timestamps included):
---
{transcript_text}
---
""".strip()


def normalize_yes_no(value: Any) -> str | None:
    token = str(value or "").strip().lower()
    if token == "yes":
        return "Yes"
    if token == "no":
        return "No"
    return None


def looks_like_transcript_quote(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False

    patterns = [
        r"\[\s*speaker_[^\]]+\]",
        r"\bspeaker[_\s]?\d+\b",
        r"\[[^\]]+\]:",
        r"\b\d{1,2}:\d{2}\b",
        r"\b--?>\b",
    ]

    if any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in patterns):
        return True

    if len(value.split()) > 70:
        return True

    return False


def compute_scoring_summary(criteria: list[dict[str, Any]]) -> dict[str, Any]:
    total_criteria = len(criteria)
    yes_count = sum(1 for item in criteria if item.get("value") == "Yes")
    no_count = total_criteria - yes_count

    critical_criteria = [item for item in criteria if item.get("is_critical") is True]
    critical_total = len(critical_criteria)
    critical_yes = sum(1 for item in critical_criteria if item.get("value") == "Yes")
    critical_no = critical_total - critical_yes

    has_critical_failure = critical_no > 0
    has_majority_failure = (yes_count * 2) < total_criteria if total_criteria > 0 else True

    if total_criteria == 0:
        pass_fail = "Fail"
        decision_reason = "Fail: no scorable criteria were produced from the rubric."
    elif has_critical_failure:
        pass_fail = "Fail"
        decision_reason = "Fail: at least one critical criterion is marked No."
    elif has_majority_failure:
        pass_fail = "Fail"
        decision_reason = "Fail: fewer than half of all criteria are marked Yes."
    else:
        pass_fail = "Pass"
        decision_reason = "Pass: all critical criteria are Yes and at least half of all criteria are Yes."

    return {
        "total_criteria": total_criteria,
        "yes_count": yes_count,
        "no_count": no_count,
        "critical_total": critical_total,
        "critical_yes": critical_yes,
        "critical_no": critical_no,
        "pass_fail": pass_fail,
        "decision_reason": decision_reason,
    }


def validate_output(
    payload: dict[str, Any],
    rubric_criteria: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    issues: list[str] = []

    criteria = payload.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        issues.append("Field 'criteria' must be a non-empty array.")
        criteria = []

    normalized_criteria: list[dict[str, Any]] = []
    if rubric_criteria:
        expected_count = len(rubric_criteria)
        if len(criteria) != expected_count:
            issues.append(
                "Criteria count must match extracted rubric criteria exactly. "
                f"Expected {expected_count}, got {len(criteria)}."
            )

        for index, expected_item in enumerate(rubric_criteria, start=1):
            source_item = criteria[index - 1] if index - 1 < len(criteria) else {}

            if not isinstance(source_item, dict):
                issues.append(f"criteria[{index}] must be an object.")
                source_item = {}

            normalized_value = normalize_yes_no(source_item.get("value"))
            if normalized_value is None:
                issues.append(f"criteria[{index}] value must be exactly Yes or No.")
                normalized_value = "No"

            reason = str(source_item.get("reason", "")).strip()
            if not reason:
                issues.append(f"criteria[{index}] missing reason.")
                reason = "No explicit evidence provided."

            timestamp_raw = source_item.get("timestamp") or source_item.get("evidence_timestamp")
            normalized_timestamp = normalize_timestamp(timestamp_raw)
            if normalized_timestamp is None:
                issues.append(f"criteria[{index}] missing or invalid timestamp (expected HH:MM:SS).")
                normalized_timestamp = "00:00:00"

            normalized_criteria.append(
                {
                    "label": str(expected_item.get("label", f"Criterion {index}")).strip() or f"Criterion {index}",
                    "is_critical": bool(expected_item.get("is_critical")),
                    "value": normalized_value,
                    "timestamp": normalized_timestamp,
                    "reason": reason,
                }
            )
    else:
        for index, item in enumerate(criteria, start=1):
            if not isinstance(item, dict):
                issues.append(f"criteria[{index}] must be an object.")
                continue

            label = str(item.get("label", "")).strip()
            if not label:
                issues.append(f"criteria[{index}] missing non-empty label.")
                label = f"Criterion {index}"

            normalized_value = normalize_yes_no(item.get("value"))
            if normalized_value is None:
                issues.append(f"criteria[{index}] value must be exactly Yes or No.")
                normalized_value = "No"

            normalized_is_critical = normalize_is_critical(item.get("is_critical"))

            reason = str(item.get("reason", "")).strip()
            if not reason:
                issues.append(f"criteria[{index}] missing reason.")
                reason = "No explicit evidence provided."

            timestamp_raw = item.get("timestamp") or item.get("evidence_timestamp")
            normalized_timestamp = normalize_timestamp(timestamp_raw)
            if normalized_timestamp is None:
                issues.append(f"criteria[{index}] missing or invalid timestamp (expected HH:MM:SS).")
                normalized_timestamp = "00:00:00"

            normalized_criteria.append(
                {
                    "label": label,
                    "is_critical": normalized_is_critical,
                    "value": normalized_value,
                    "timestamp": normalized_timestamp,
                    "reason": reason,
                }
            )

    keep_start_stop = payload.get("keep_start_stop")
    if not isinstance(keep_start_stop, dict):
        issues.append("Field 'keep_start_stop' must be an object.")
        keep_start_stop = {}

    normalized_kss: dict[str, str] = {}
    for key in ("keep", "start", "stop"):
        text = str(keep_start_stop.get(key, "")).strip()
        if not text:
            issues.append(f"keep_start_stop.{key} must contain at least one sentence.")
            text = "Insufficient detail provided to generate this feedback item."
        elif looks_like_transcript_quote(text):
            issues.append(
                f"keep_start_stop.{key} must be coaching feedback for the student, not transcript quotes or speaker-tag snippets."
            )
        normalized_kss[key] = text

    normalized_payload = {
        "session_id": str(payload.get("session_id", "")).strip(),
        "transcript_file": str(payload.get("transcript_file", "")).strip(),
        "case_study_file": str(payload.get("case_study_file", "")).strip(),
        "rubric_file": str(payload.get("rubric_file", "")).strip() or "embedded_in_case_study_pdf",
        "criteria": normalized_criteria,
        "keep_start_stop": normalized_kss,
        "overall_summary": str(payload.get("overall_summary", "")).strip(),
    }

    if not normalized_payload["overall_summary"]:
        issues.append("Field 'overall_summary' must be a non-empty string.")
        normalized_payload["overall_summary"] = "Summary unavailable."

    return normalized_payload, issues


def build_follow_up_messages(
    base_messages: list[dict[str, Any]],
    first_message: Any,
    issues: list[str],
) -> list[dict[str, Any]]:
    assistant_message: dict[str, Any] = {
        "role": "assistant",
        "content": first_message.content or "",
    }

    reasoning_details = getattr(first_message, "reasoning_details", None)
    if reasoning_details is not None:
        assistant_message["reasoning_details"] = reasoning_details

    issue_lines = "\n".join(f"- {issue}" for issue in issues)
    repair_instruction = (
        "Your previous response failed validation. Return corrected JSON only, with no markdown.\n"
        "Fix all issues below and keep your reasoning evidence-based:\n"
        f"{issue_lines}"
    )

    return [*base_messages, assistant_message, {"role": "user", "content": repair_instruction}]


def to_repo_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT_DIR))
    except ValueError:
        return str(path.resolve())


__all__ = [
    "MAX_CASE_STUDY_CONTEXT_CHARS",
    "MAX_RUBRIC_SECTION_CHARS",
    "MAX_TRANSCRIPT_CHARS",
    "MIN_VALID_CONTENT_CHARS",
    "PROMPT_VERSION",
    "ROOT_DIR",
    "build_follow_up_messages",
    "build_system_prompt",
    "build_user_prompt",
    "clean_rubric_label",
    "compute_scoring_summary",
    "extract_rubric_criteria_from_case_study_rubric",
    "looks_like_transcript_quote",
    "normalize_is_critical",
    "normalize_yes_no",
    "read_file_as_context_text",
    "read_pdf_text",
    "read_srt_transcript_text",
    "to_repo_relative",
    "validate_output",
]
