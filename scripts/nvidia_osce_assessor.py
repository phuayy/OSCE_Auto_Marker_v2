#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from env_loader import load_env_file
from llm_bootstrap import (
    ChatResponse,
    LLMRouter,
    build_chat_request,
    build_router_from_env,
    describe_routing,
    validator_from,
)
from rubric_section import (
    diagnose_missing_rubric_section,
    split_case_study_context_and_rubric,
)


ROOT_DIR = Path(__file__).resolve().parents[1]
load_env_file(ROOT_DIR)
STORAGE_DIR = ROOT_DIR / "storage"
SESSIONS_DIR = STORAGE_DIR / "sessions"
WHISPERX_OUTPUT_DIR = STORAGE_DIR / "output" / "whisperx"
NORMALIZED_TRANSCRIPTS_DIR = STORAGE_DIR / "output" / "transcripts"
UPLOADED_CASE_STUDIES_DIR = STORAGE_DIR / "input" / "case_studies"
FALLBACK_CASE_STUDIES_DIR = ROOT_DIR / "case_studies"
SCORES_OUTPUT_DIR = STORAGE_DIR / "output" / "scores"

# Which provider and model run is no longer decided here. The API resolves the
# operator's primary/fallback choice from the settings database and passes it in
# OSCE_LLM_ROUTING; running this script by hand with no routing variable falls
# back to the legacy NVIDIA_MODEL_NAME / NVIDIA_FALLBACK_MODELS behaviour. See
# scripts/llm_bootstrap.py and fastapi_backend/app/llm/.

MAX_CASE_STUDY_CONTEXT_CHARS = 36_000
MAX_RUBRIC_SECTION_CHARS = 48_000
MAX_TRANSCRIPT_CHARS = 60_000
# Empty/very short content (below this many characters) is treated as a truncated
# response and triggers a retry instead of falling through to "default everything
# to No / no evidence" in the validator.
MIN_VALID_CONTENT_CHARS = 40
CHECKPOINT_SCHEMA = "nvidia-osce-assessor-checkpoint-v1"


class CheckpointMessage:
    """A completion restored from disk after a crash.

    Stands in for a live provider response, so the repair loop cannot tell the
    difference between resuming and having just made the call. It records which
    model produced the text as well: the output file names the model that
    actually scored the student, and a resumed run must not relabel that as
    whatever is configured today.
    """

    def __init__(self, content: str, model: str = "", provider_id: str = "") -> None:
        self.content = content
        self.model = model
        self.provider_id = provider_id


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(text, encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def checkpoint_path_for_output(output_path: Path) -> Path:
    return output_path.with_name(f".{output_path.name}.checkpoint.json")


def read_checkpoint(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def checkpoint_matches(payload: dict[str, Any] | None, context: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("schema") != CHECKPOINT_SCHEMA:
        return False
    saved_context = payload.get("context")
    return isinstance(saved_context, dict) and saved_context == context


def write_checkpoint(path: Path | None, context: dict[str, Any], state: dict[str, Any]) -> None:
    if path is None:
        return
    payload = {
        "schema": CHECKPOINT_SCHEMA,
        "context": context,
        "state": state,
        "updated_at": time.time(),
    }
    write_text_atomic(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def checkpoint_file_signature(path: Path) -> dict[str, Any]:
    stats = path.stat()
    return {
        "path": str(path.resolve()),
        "size": stats.st_size,
        "mtime_ns": stats.st_mtime_ns,
    }


# Sampling knobs (temperature, top_p, max_tokens, request timeout, reasoning)
# are read from the environment by app.llm.runtime.request_defaults_from_env, so
# every LLM caller in this project honours the same variables. The historical
# NVIDIA_* names still work; the vendor-neutral LLM_* names take precedence.
#
# The defaults that matter and why:
#   temperature 0.2  — 1.0 let the model ramble past its token budget even with
#                      response_format=json_object.
#   max_tokens 24576 — headroom for ~25 rubric criteria without truncation.
#   thinking off     — a hidden reasoning trace is billed against max_tokens and
#                      returned empty content, which scored every criterion "No".


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate rubric-aligned OSCE Yes/No scoring JSON from transcript and case study files "
            "using NVIDIA API."
        )
    )
    parser.add_argument("--session-id", help="Session ID (defaults to latest session metadata file)")
    parser.add_argument(
        "--transcript",
        help=(
            "Transcript path override. If omitted, uses "
            "storage/output/whisperx/<session-id>/<session-id>.srt"
        ),
    )
    parser.add_argument(
        "--case-study",
        help=(
            "Case study file override (PDF expected). If omitted, tries latest uploaded "
            "case study in storage/input/case_studies, then falls back to case_studies/*.pdf"
        ),
    )
    parser.add_argument(
        "--output",
        help="Output JSON path (defaults to storage/output/scores/<session-id>.json)",
    )
    parser.add_argument(
        "--stdout-only",
        action="store_true",
        help="Print JSON to stdout and skip writing output file",
    )
    return parser.parse_args()


def newest_file(directory: Path, pattern: str) -> Path | None:
    if not directory.exists():
        return None

    candidates = [p for p in directory.glob(pattern) if p.is_file()]
    if not candidates:
        return None

    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def session_id_from_latest_metadata() -> str:
    latest = newest_file(SESSIONS_DIR, "*.json")
    if latest is None:
        raise FileNotFoundError(
            f"No session metadata found in {SESSIONS_DIR}. Provide --session-id or --transcript explicitly."
        )
    return latest.stem


def read_session_metadata(session_id: str) -> dict[str, Any] | None:
    session_path = SESSIONS_DIR / f"{session_id}.json"
    if not session_path.exists():
        return None

    return json.loads(session_path.read_text(encoding="utf-8"))


def resolve_transcript_path(session_id: str, transcript_override: str | None) -> Path:
    if transcript_override:
        transcript_path = Path(transcript_override).expanduser().resolve()
        if not transcript_path.exists():
            raise FileNotFoundError(f"Transcript override does not exist: {transcript_path}")
        return transcript_path

    session_folder = WHISPERX_OUTPUT_DIR / session_id

    expected_srt = session_folder / f"{session_id}.srt"
    if expected_srt.exists():
        return expected_srt

    fallback_srt = newest_file(session_folder, "*.srt") if session_folder.exists() else None
    if fallback_srt:
        return fallback_srt

    expected_vtt = session_folder / f"{session_id}.vtt"
    if expected_vtt.exists():
        return expected_vtt

    fallback_vtt = newest_file(session_folder, "*.vtt") if session_folder.exists() else None
    if fallback_vtt:
        return fallback_vtt

    expected_txt = session_folder / f"{session_id}.txt"
    if expected_txt.exists():
        return expected_txt

    fallback_txt = newest_file(session_folder, "*.txt") if session_folder.exists() else None
    if fallback_txt:
        return fallback_txt

    expected_json = session_folder / f"{session_id}.json"
    if expected_json.exists():
        return expected_json

    fallback_json = newest_file(session_folder, "*.json") if session_folder.exists() else None
    if fallback_json:
        return fallback_json

    normalized_transcript_json = NORMALIZED_TRANSCRIPTS_DIR / f"{session_id}.json"
    if normalized_transcript_json.exists():
        return normalized_transcript_json

    raise FileNotFoundError(
        "Could not resolve transcript file. Expected "
        f"{expected_srt} or another .srt/.vtt/.txt/.json inside {session_folder}, "
        f"or normalized transcript {normalized_transcript_json}."
    )


def resolve_case_study_path(
    case_study_override: str | None,
    session_metadata: dict[str, Any] | None,
) -> Path:
    if case_study_override:
        case_study_path = Path(case_study_override).expanduser().resolve()
        if not case_study_path.exists():
            raise FileNotFoundError(f"Case study override does not exist: {case_study_path}")
        return case_study_path

    case_study_from_session = (
        session_metadata
        and session_metadata.get("files", {})
        and session_metadata["files"].get("caseStudy", {})
        and session_metadata["files"]["caseStudy"].get("absolutePath")
    )
    if case_study_from_session:
        case_study_path = Path(str(case_study_from_session))
        if case_study_path.exists():
            return case_study_path

    uploaded_case_study = newest_file(UPLOADED_CASE_STUDIES_DIR, "*.pdf")
    if uploaded_case_study:
        return uploaded_case_study

    fallback_case_study = newest_file(FALLBACK_CASE_STUDIES_DIR, "*.pdf")
    if fallback_case_study:
        return fallback_case_study

    raise FileNotFoundError(
        "No case study PDF found. Provide --case-study, upload one to storage/input/case_studies, "
        f"or place one in {FALLBACK_CASE_STUDIES_DIR}."
    )


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


def read_generic_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1", errors="ignore")


SRT_TIMESTAMP_PATTERN = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2}[,\.]\d{3})"
)


def format_timestamp_seconds(seconds: float, include_ms: bool = True) -> str:
    safe_seconds = max(0.0, float(seconds or 0.0))
    total_ms = int(round(safe_seconds * 1000))
    hours = total_ms // 3_600_000
    minutes = (total_ms % 3_600_000) // 60_000
    whole_seconds = (total_ms % 60_000) // 1_000
    milliseconds = total_ms % 1_000

    if include_ms:
        return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}"


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


def read_json_transcript_text(path: Path) -> str:
    raw_text = read_generic_text(path)

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return raw_text

    if isinstance(parsed, dict):
        segments = parsed.get("segments")
        if isinstance(segments, list):
            lines: list[str] = []
            def to_float(value: Any) -> float | None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None

            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                text = str(segment.get("text", "")).strip()
                if not text:
                    continue
                speaker = str(segment.get("speaker", "SPEAKER_UNKNOWN")).strip() or "SPEAKER_UNKNOWN"
                start_value = to_float(segment.get("start"))
                end_value = to_float(segment.get("end"))
                start_label = str(segment.get("startLabel") or "").strip()
                end_label = str(segment.get("endLabel") or "").strip()

                if start_value is not None:
                    start_label = format_timestamp_seconds(start_value)
                if end_value is not None:
                    end_label = format_timestamp_seconds(end_value)

                if start_label and end_label:
                    timestamp_label = f"{start_label} - {end_label}"
                elif start_label:
                    timestamp_label = start_label
                else:
                    timestamp_label = ""

                if timestamp_label:
                    lines.append(f"[{speaker}] {timestamp_label}: {text}")
                else:
                    lines.append(f"[{speaker}]: {text}")

            if lines:
                return "\n".join(lines)

    return json.dumps(parsed, indent=2, ensure_ascii=False)


def read_file_as_context_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return read_pdf_text(path)
    if suffix in {".srt", ".vtt"}:
        return read_srt_transcript_text(path)
    if suffix == ".json":
        return read_json_transcript_text(path)
    return read_generic_text(path)


def clip_text(value: str, max_chars: int, label: str) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text

    kept = text[:max_chars]
    return f"{kept}\n\n[TRUNCATED {label}: original_length={len(text)} chars, kept={max_chars}]"


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


def format_response_content_for_log(content: str, max_chars: int = 4000) -> str:
    text = str(content or "")
    if not text:
        return "(empty)"
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}... [truncated, total {len(text)} chars]"


def extract_primary_json_dict_from_model_output(raw_text: str) -> dict[str, Any]:
    """Prefer the richest JSON dict that contains `criteria` (fixes first-{ … last-}
    slicing when the assistant emits analysis text before / after JSON)."""

    text = str(raw_text or "").strip()
    if not text:
        raise ValueError("Model returned empty content.")

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        raise ValueError("Top-level JSON was not an object.")
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    dict_candidates: list[dict[str, Any]] = []
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            candidate, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            dict_candidates.append(candidate)

    if not dict_candidates:
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace < 0 or last_brace <= first_brace:
            raise ValueError("Model output did not contain a JSON object.")
        try:
            parsed = json.loads(text[first_brace : last_brace + 1])
        except json.JSONDecodeError as error:
            raise ValueError("Could not decode JSON object from model output.") from error
        if not isinstance(parsed, dict):
            raise ValueError("Extracted JSON was not an object.")
        return parsed

    with_criteria = [item for item in dict_candidates if isinstance(item.get("criteria"), list)]
    ranked = with_criteria or dict_candidates

    def rank_key(item: dict[str, Any]) -> tuple[int, int]:
        crit = item.get("criteria") if isinstance(item.get("criteria"), list) else None
        return (len(crit) if crit is not None else -1, len(json.dumps(item, ensure_ascii=False)))

    return max(ranked, key=rank_key)


def enforce_expected_criteria_array_or_raise_retry(raw_content: str, expected_len: int | None) -> None:
    """`response_format=json_object` occasionally returns `{}` / `{\"schema\":\"\"}`
    scaffolding. Retry at the HTTP layer instead of burning repair rounds."""

    if not expected_len or expected_len <= 0:
        return

    try:
        data = extract_primary_json_dict_from_model_output(raw_content)
    except Exception:
        return

    crit = data.get("criteria")
    if not isinstance(crit, list):
        raise RuntimeError(
            "Model JSON had missing or non-array field 'criteria' (often a provider-side "
            f"structured-output scaffolding bug). This is retryable. "
            f"response_content={format_response_content_for_log(raw_content)!r}"
        )

    if len(crit) != expected_len:
        raise RuntimeError(
            "Model JSON had an incomplete criteria array "
            f"(expected {expected_len} items for this rubric, got {len(crit)}). This is retryable. "
            f"response_content={format_response_content_for_log(raw_content)!r}"
        )


def create_completion(
    router: LLMRouter,
    messages: list[dict[str, Any]],
    *,
    expected_rubric_items: int | None = None,
) -> ChatResponse:
    """One scored completion, across whichever providers are configured.

    Everything this function used to do by hand — the model fallback list, the
    request-shape ladder, the backoff, the retryability rules — now lives in the
    shared router, so the communication scorer and the transcript preprocessor
    behave identically without a second copy of it.

    What stays here is the part only this script knows: a response whose
    ``criteria`` array is missing or short is a provider-side structured-output
    bug, not a scoring result, and must be retried rather than validated into a
    sheet of defaults.
    """

    def check(content: str) -> None:
        enforce_expected_criteria_array_or_raise_retry(content, expected_rubric_items)

    request = build_chat_request(
        messages,
        label="content-scoring",
        min_content_chars=MIN_VALID_CONTENT_CHARS,
    )
    return router.complete(request, validate=validator_from(check))


def extract_json_from_text(raw_text: str) -> dict[str, Any]:
    return extract_primary_json_dict_from_model_output(raw_text)


def safe_extract_payload(raw_text: str) -> tuple[dict[str, Any], str | None]:
    try:
        return extract_json_from_text(raw_text), None
    except Exception as error:
        return {}, f"Model JSON parse error: {error}"


def normalize_yes_no(value: Any) -> str | None:
    token = str(value or "").strip().lower()
    if token == "yes":
        return "Yes"
    if token == "no":
        return "No"
    return None


TIMESTAMP_HMS_PATTERN = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})(?:[\.,](\d{1,3}))?")
TIMESTAMP_MS_PATTERN = re.compile(r"(\d{1,2}):(\d{2})(?:[\.,](\d{1,3}))?")


def normalize_timestamp(value: Any) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return format_timestamp_seconds(float(value), include_ms=False)

    text = str(value or "").strip()
    if not text:
        return None

    match = TIMESTAMP_HMS_PATTERN.search(text)
    if match:
        hours = int(match.group(1))
        minutes = int(match.group(2))
        seconds = int(match.group(3))
        millis = int(match.group(4) or 0)
        total_seconds = hours * 3600 + minutes * 60 + seconds + (millis / 1000)
        return format_timestamp_seconds(total_seconds, include_ms=False)

    match = TIMESTAMP_MS_PATTERN.search(text)
    if match:
        minutes = int(match.group(1))
        seconds = int(match.group(2))
        millis = int(match.group(3) or 0)
        total_seconds = minutes * 60 + seconds + (millis / 1000)
        return format_timestamp_seconds(total_seconds, include_ms=False)

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


def parse_session_id(args: argparse.Namespace) -> str:
    if args.session_id:
        return str(args.session_id).strip()

    if args.transcript:
        transcript_name = Path(args.transcript).stem.strip()
        if transcript_name:
            return transcript_name

    return session_id_from_latest_metadata()


def main() -> int:
    args = parse_args()
    router = build_router_from_env()
    routing_summary = describe_routing(router)
    print(f"[nvidia_osce_assessor] LLM routing: {routing_summary}", file=sys.stderr)

    session_id = parse_session_id(args)
    output_path = None if args.stdout_only else (
        Path(args.output).expanduser().resolve() if args.output else SCORES_OUTPUT_DIR / f"{session_id}.json"
    )
    checkpoint_path = checkpoint_path_for_output(output_path) if output_path is not None else None
    session_metadata = read_session_metadata(session_id)

    transcript_path = resolve_transcript_path(session_id, args.transcript)
    case_study_path = resolve_case_study_path(args.case_study, session_metadata)

    transcript_text = clip_text(read_file_as_context_text(transcript_path), MAX_TRANSCRIPT_CHARS, "transcript")

    case_study_full_text = read_file_as_context_text(case_study_path)
    case_study_context_text, rubric_section_text = split_case_study_context_and_rubric(case_study_full_text)

    if not rubric_section_text:
        raise ValueError(diagnose_missing_rubric_section(case_study_full_text))

    rubric_criteria = extract_rubric_criteria_from_case_study_rubric(rubric_section_text)
    if len(rubric_criteria) < 2:
        raise ValueError(
            "Could not extract enough rubric criteria from case-study PDF rubric section. "
            f"Found {len(rubric_criteria)} criteria."
        )

    checkpoint_context = {
        "session_id": session_id,
        # Part of the checkpoint fingerprint: changing the configured model
        # must invalidate a half-finished run rather than silently splicing
        # one model's output into another's.
        "model": routing_summary,
        "transcript": checkpoint_file_signature(transcript_path),
        "case_study": checkpoint_file_signature(case_study_path),
        "rubric_criteria_count": len(rubric_criteria),
    }
    checkpoint_payload = read_checkpoint(checkpoint_path)
    checkpoint_state = (
        checkpoint_payload.get("state")
        if checkpoint_matches(checkpoint_payload, checkpoint_context)
        and isinstance(checkpoint_payload.get("state"), dict)
        else None
    )

    case_study_context_text = clip_text(
        case_study_context_text or case_study_full_text,
        MAX_CASE_STUDY_CONTEXT_CHARS,
        "case_study_context",
    )
    rubric_section_text = clip_text(rubric_section_text, MAX_RUBRIC_SECTION_CHARS, "rubric_section")

    system_prompt = build_system_prompt(rubric_criteria)
    user_prompt = build_user_prompt(
        session_id=session_id,
        transcript_path=transcript_path,
        case_study_path=case_study_path,
        rubric_criteria=rubric_criteria,
        transcript_text=transcript_text,
        case_study_context_text=case_study_context_text,
        rubric_section_text=rubric_section_text,
    )

    base_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


    def is_useful_payload(candidate: dict[str, Any], remaining_issues: list[str]) -> bool:
        """A scoring payload is only treated as 'useful' if at least one criterion
        has a non-default reason. The validator silently falls back to
        value='No', reason='No explicit evidence provided.', timestamp='00:00:00'
        when the model produced nothing — we used to ship that as a real result.
        """
        if remaining_issues:
            return False
        criteria_list = candidate.get("criteria") or []
        if not isinstance(criteria_list, list) or not criteria_list:
            return False
        fallback_reason = "No explicit evidence provided."
        for criterion in criteria_list:
            if not isinstance(criterion, dict):
                continue
            reason = str(criterion.get("reason", "")).strip()
            if reason and reason != fallback_reason:
                return True
        return False

    # Make at most TWO repair attempts. Real markers fix typos before grading; we
    # mirror that with two structured re-prompts that include the issue list.
    max_repair_attempts = 2
    if checkpoint_state:
        checkpoint_stage = str(checkpoint_state.get("stage") or "")
        normalized_payload = checkpoint_state.get("normalized_payload") or {}
        issues = checkpoint_state.get("issues") or []
        repair_attempts = int(checkpoint_state.get("repair_attempts") or 0)
        last_message = CheckpointMessage(
            str(checkpoint_state.get("last_message_content") or ""),
            model=str(checkpoint_state.get("last_message_model") or ""),
            provider_id=str(checkpoint_state.get("last_message_provider") or ""),
        )
        if not last_message.content:
            checkpoint_state = None
        elif checkpoint_stage in {"first_completion_returned", "repair_completion_returned"}:
            checkpoint_payload_candidate, parse_error = safe_extract_payload(last_message.content)
            normalized_payload, issues = validate_output(checkpoint_payload_candidate, rubric_criteria)
            if parse_error:
                issues = [parse_error, *issues]
            write_checkpoint(
                checkpoint_path,
                checkpoint_context,
                {
                    "stage": checkpoint_stage.replace("_returned", "_validated"),
                    "repair_attempts": repair_attempts,
                    "last_message_content": last_message.content,
                "last_message_model": getattr(last_message, "model", ""),
                "last_message_provider": getattr(last_message, "provider_id", ""),
                    "normalized_payload": normalized_payload,
                    "issues": issues,
                },
            )
        elif not isinstance(normalized_payload, dict) or not isinstance(issues, list):
            checkpoint_state = None

    if not checkpoint_state:
        first_message = create_completion(router, base_messages, expected_rubric_items=len(rubric_criteria))
        write_checkpoint(
            checkpoint_path,
            checkpoint_context,
            {
                "stage": "first_completion_returned",
                "repair_attempts": 0,
                "last_message_content": first_message.content or "",
                "last_message_model": first_message.model,
                "last_message_provider": first_message.provider_id,
            },
        )
        first_payload, parse_error = safe_extract_payload(first_message.content or "")
        normalized_payload, issues = validate_output(first_payload, rubric_criteria)
        if parse_error:
            issues = [parse_error, *issues]
        repair_attempts = 0
        last_message = first_message
        write_checkpoint(
            checkpoint_path,
            checkpoint_context,
            {
                "stage": "first_completion_validated",
                "repair_attempts": repair_attempts,
                "last_message_content": last_message.content or "",
                "last_message_model": getattr(last_message, "model", ""),
                "last_message_provider": getattr(last_message, "provider_id", ""),
                "normalized_payload": normalized_payload,
                "issues": issues,
            },
        )

    while issues and repair_attempts < max_repair_attempts:
        repair_attempts += 1
        follow_up_messages = build_follow_up_messages(base_messages, last_message, issues)
        try:
            next_message = create_completion(router, follow_up_messages, expected_rubric_items=len(rubric_criteria))
        except Exception as repair_error:
            issues.append(f"Repair attempt {repair_attempts} failed: {repair_error}")
            print(
                f"[nvidia_osce_assessor] repair attempt {repair_attempts} aborted: {repair_error}",
                file=sys.stderr,
            )
            break

        last_message = next_message
        write_checkpoint(
            checkpoint_path,
            checkpoint_context,
            {
                "stage": "repair_completion_returned",
                "repair_attempts": repair_attempts,
                "last_message_content": last_message.content or "",
                "last_message_model": getattr(last_message, "model", ""),
                "last_message_provider": getattr(last_message, "provider_id", ""),
                "normalized_payload": normalized_payload,
                "issues": issues,
            },
        )
        next_payload, parse_error = safe_extract_payload(next_message.content or "")
        next_normalized, next_issues = validate_output(next_payload, rubric_criteria)
        if parse_error:
            next_issues = [parse_error, *next_issues]
        normalized_payload = next_normalized
        issues = next_issues
        write_checkpoint(
            checkpoint_path,
            checkpoint_context,
            {
                "stage": "repair_completion_validated",
                "repair_attempts": repair_attempts,
                "last_message_content": last_message.content or "",
                "last_message_model": getattr(last_message, "model", ""),
                "last_message_provider": getattr(last_message, "provider_id", ""),
                "normalized_payload": normalized_payload,
                "issues": issues,
            },
        )
        if not issues:
            break

    if issues:
        normalized_payload["warnings"] = issues

    # If the validator could not produce any real evidence (model returned empty
    # content or pure defaults), refuse to silently ship the all-zero fallback —
    # the server treats a non-zero exit as a hard failure that triggers a re-run
    # rather than committing a misleading result.
    if not is_useful_payload(normalized_payload, issues):
        diagnostic = (
            "NVIDIA content scoring returned no usable output after all retries and "
            "two repair passes. This usually means the model truncated its response "
            "(finish_reason=length) or returned no JSON. Investigate the model "
            "provider for rate-limit / capacity issues."
        )
        if issues:
            diagnostic += " Validator issues: " + "; ".join(str(item) for item in issues[:5])
        print(f"[nvidia_osce_assessor] {diagnostic}", file=sys.stderr)
        raise RuntimeError(diagnostic)

    normalized_payload["session_id"] = session_id
    normalized_payload["transcript_file"] = to_repo_relative(transcript_path)
    normalized_payload["case_study_file"] = to_repo_relative(case_study_path)
    normalized_payload["rubric_file"] = "embedded_in_case_study_pdf"
    normalized_payload["rubric_source"] = f"{to_repo_relative(case_study_path)}#rubric-section"
    # The model that actually produced these marks, which after a fallback is
    # not necessarily the configured primary.
    normalized_payload["model"] = getattr(last_message, "model", "") or routing_summary
    normalized_payload["model_provider"] = getattr(last_message, "provider_id", "")
    normalized_payload["scoring_summary"] = compute_scoring_summary(normalized_payload["criteria"])
    normalized_payload["path_checks"] = {
        "transcript_folder_matches_file_stem": transcript_path.parent.name == transcript_path.stem
    }

    output_json = json.dumps(normalized_payload, indent=2, ensure_ascii=False)

    if output_path is not None:
        write_text_atomic(output_path, output_json + "\n")
        if checkpoint_path is not None:
            checkpoint_path.unlink(missing_ok=True)
        print(f"Saved: {output_path}", file=sys.stderr)

    print(output_json)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
