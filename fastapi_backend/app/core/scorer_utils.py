from __future__ import annotations

import json
import math
import re
from pathlib import Path

TIMESTAMP_HMS_PATTERN = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})(?:[\.,](\d{1,3}))?")
TIMESTAMP_MS_PATTERN = re.compile(r"(\d{1,2}):(\d{2})(?:[\.,](\d{1,3}))?")


def read_generic_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1", errors="ignore")


def format_timestamp_seconds(seconds: float, include_ms: bool = True) -> str:
    safe_seconds = max(0.0, float(seconds or 0.0))
    if not math.isfinite(safe_seconds):
        raise ValueError("Timestamp must be finite.")
    total_ms = int(round(safe_seconds * 1000))
    hours = total_ms // 3_600_000
    minutes = (total_ms % 3_600_000) // 60_000
    whole_seconds = (total_ms % 60_000) // 1_000
    milliseconds = total_ms % 1_000
    base = f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}"
    return f"{base}.{milliseconds:03d}" if include_ms else base


def read_json_transcript_text(path: Path, *, include_ms: bool = True) -> str:
    raw_text = read_generic_text(path)
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return raw_text
    segments = parsed.get("segments") if isinstance(parsed, dict) else None
    lines = []
    for segment in segments if isinstance(segments, list) else []:
        if not isinstance(segment, dict):
            continue
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        speaker = str(segment.get("speaker", "SPEAKER_UNKNOWN")).strip() or "SPEAKER_UNKNOWN"
        labels = []
        for key in ("start", "end"):
            label = str(segment.get(f"{key}Label") or "").strip()
            try:
                label = format_timestamp_seconds(float(segment[key]), include_ms=include_ms)
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
            labels.append(label)
        start, end = labels
        timestamp = f"{start} - {end}" if start and end else start
        prefix = f"[{speaker}] {timestamp}" if timestamp else f"[{speaker}]"
        lines.append(f"{prefix}: {text}")
    return "\n".join(lines) if lines else json.dumps(parsed, indent=2, ensure_ascii=False)


def clip_text(value: str, max_chars: int, label: str) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n\n[TRUNCATED {label}: original_length={len(text)} chars, kept={max_chars}]"


def format_response_content_for_log(content: str, max_chars: int = 4000) -> str:
    text = str(content or "")
    if not text:
        return "(empty)"
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}... [truncated, total {len(text)} chars]"


def extract_primary_json_dict_from_model_output(raw_text: str) -> dict:
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
    candidates = []
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            candidate, _end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            candidates.append(candidate)
    if not candidates:
        raise ValueError("Model output did not contain a JSON object.")
    with_criteria = [item for item in candidates if isinstance(item.get("criteria"), list)]

    def rank_key(item: dict) -> tuple[int, int]:
        criteria = item.get("criteria")
        return (len(criteria) if isinstance(criteria, list) else -1, len(json.dumps(item, ensure_ascii=False)))

    return max(with_criteria or candidates, key=rank_key)


extract_json_from_text = extract_primary_json_dict_from_model_output


def enforce_expected_criteria_array(raw_content: str, expected_len: int | None) -> None:
    if not expected_len or expected_len <= 0:
        return
    try:
        data = extract_primary_json_dict_from_model_output(raw_content)
    except ValueError:
        return
    criteria = data.get("criteria")
    if not isinstance(criteria, list):
        raise RuntimeError(
            "Model JSON had missing or non-array field 'criteria' (often a provider-side "
            "structured-output scaffolding bug). This is retryable. "
            f"response_content={format_response_content_for_log(raw_content)!r}"
        )
    if len(criteria) != expected_len:
        raise RuntimeError(
            "Model JSON had an incomplete criteria array "
            f"(expected {expected_len} items for this rubric, got {len(criteria)}). This is retryable. "
            f"response_content={format_response_content_for_log(raw_content)!r}"
        )


def safe_extract_payload(raw_text: str) -> tuple[dict, str | None]:
    try:
        return extract_json_from_text(raw_text), None
    except (ValueError, TypeError) as error:
        return {}, f"Model JSON parse error: {error}"


def normalize_timestamp(value: object) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return format_timestamp_seconds(float(value), include_ms=False) if math.isfinite(value) else None
    text = str(value or "").strip()
    for pattern, has_hours in [(TIMESTAMP_HMS_PATTERN, True), (TIMESTAMP_MS_PATTERN, False)]:
        match = pattern.search(text)
        if match:
            parts = match.groups()
            hours = int(parts[0]) if has_hours else 0
            minutes, seconds, fraction = parts[1:] if has_hours else parts
            total = hours * 3600 + int(minutes) * 60 + int(seconds) + float(f"0.{fraction or '0'}")
            return format_timestamp_seconds(total, include_ms=False)
    return None
