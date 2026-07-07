#!/usr/bin/env python3
"""Communication rubric scorer (NVIDIA).

This scorer takes the parsed communication rubric JSON (produced by
scripts/parse_communication_rubric.py) and asks the model to assign each
criterion one of {"None", "Some", "Most", "All"} based on transcript evidence.

The model never sees raw point values. The mapping
    All  = 3 marks
    Most = 2 marks
    Some = 1 mark
    None = 0 marks
is applied server-side after the model returns its qualitative label, so the
scoring stays loyal to the rubric's intent and so future criteria (e.g. #2
using openSMILE prosody features) can be plugged into the same pipeline.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from openai import OpenAI

from env_loader import load_env_file

ROOT_DIR = Path(__file__).resolve().parents[1]
load_env_file(ROOT_DIR)
STORAGE_DIR = ROOT_DIR / "storage"
SESSIONS_DIR = STORAGE_DIR / "sessions"
TRANSCRIPTS_DIR = STORAGE_DIR / "output" / "transcripts"
AUDIO_PROF_DIR = STORAGE_DIR / "output" / "audio_professionalism"
COMM_SCORES_DIR = STORAGE_DIR / "output" / "communication_scores"
AUTH_DIR = STORAGE_DIR / "auth"
PARSED_RUBRIC_PATH = AUTH_DIR / "communication_rubric.json"

NVIDIA_BASE_URL = (
    os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1").strip()
    or "https://integrate.api.nvidia.com/v1"
)

MODEL_NAME = (
    os.getenv("NVIDIA_COMM_MODEL_NAME")
    or os.getenv("NVIDIA_MODEL_NAME", "nvidia/nemotron-3-super-120b-a12b")
).strip()

MAX_TRANSCRIPT_CHARS = 60_000
MAX_AUDIO_CONTEXT_CHARS = 24_000
# Cap for the formatted OpenSMILE feature dump that gets injected separately
# into the user prompt. Most eGeMAPSv02 dumps are well under 20 kB.
MAX_OPEN_SMILE_CONTEXT_CHARS = 24_000
MAX_COMPLETION_RETRIES_PER_MODE = 3
RETRYABLE_PROVIDER_ERROR_CODES = {408, 409, 425, 429, 500, 502, 503, 504, 522, 524}
# See nvidia_osce_assessor.py — same defensive minimum, same reasoning limits.
MIN_VALID_CONTENT_CHARS = 40

SCORE_LABEL_TO_POINTS = {
    "All": 3,
    "Most": 2,
    "Some": 1,
    "None": 0,
}
ALLOWED_SCORE_LABELS = list(SCORE_LABEL_TO_POINTS.keys())


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(text, encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def read_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def read_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def read_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    token = str(raw).strip().lower()
    if token in {"1", "true", "yes", "y", "on"}:
        return True
    if token in {"0", "false", "no", "n", "off"}:
        return False
    return default


# See nvidia_osce_assessor.py for the rationale behind these defaults.
NVIDIA_TEMPERATURE = read_float_env("NVIDIA_TEMPERATURE", 0.2)
NVIDIA_TOP_P = read_float_env("NVIDIA_TOP_P", 0.9)
NVIDIA_MAX_TOKENS = read_int_env("NVIDIA_MAX_TOKENS", 24_576)
# Communication scorer reasoning is independent of the clinical script's NVIDIA_ENABLE_THINKING.
NVIDIA_COMMUNICATION_ENABLE_THINKING = read_bool_env("NVIDIA_COMMUNICATION_ENABLE_THINKING", True)
NVIDIA_REASONING_BUDGET = read_int_env("NVIDIA_REASONING_BUDGET", 16384)
REQUEST_TIMEOUT_SECONDS = read_int_env("NVIDIA_REQUEST_TIMEOUT_SECONDS", 5000)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate communications rubric scoring JSON. Uses the parsed rubric JSON at "
            f"{PARSED_RUBRIC_PATH} (override with --parsed-rubric)."
        )
    )
    parser.add_argument("--session-id", help="Session ID (defaults to latest session metadata file)")
    parser.add_argument("--transcript", help="Transcript JSON path override")
    parser.add_argument("--audio-professionalism", help="Audio professionalism JSON path override")
    parser.add_argument(
        "--parsed-rubric",
        help=(
            "Parsed communication rubric JSON path override. Defaults to "
            "storage/auth/communication_rubric.json."
        ),
    )
    parser.add_argument(
        "--rubric",
        help=(
            "[deprecated] Source rubric PDF path. Ignored when a parsed JSON is available. "
            "Kept for backward compatibility with older callers."
        ),
    )
    parser.add_argument(
        "--output",
        help="Output JSON path (defaults to storage/output/communication_scores/<session-id>.json)",
    )
    parser.add_argument(
        "--stdout-only",
        action="store_true",
        help="Print JSON to stdout and skip writing --output.",
    )
    return parser.parse_args()


def require_api_key() -> str:
    key = os.getenv("NVIDIA_API_KEY", "").strip()
    if not key or key == "<NVIDIA_API_KEY>":
        raise RuntimeError(
            "NVIDIA API key missing. The local server reads it from storage/auth/secrets.json "
            "and passes it as the NVIDIA_API_KEY environment variable; set the key there or "
            "export NVIDIA_API_KEY in your shell before running this script directly."
        )
    return key


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
            f"No session metadata found in {SESSIONS_DIR}. Provide --session-id explicitly."
        )
    return latest.stem


def read_session_metadata(session_id: str) -> dict[str, Any] | None:
    session_path = SESSIONS_DIR / f"{session_id}.json"
    if not session_path.exists():
        return None

    return json.loads(session_path.read_text(encoding="utf-8"))


def resolve_transcript_path(
    session_id: str,
    transcript_override: str | None,
    session_metadata: dict[str, Any] | None,
) -> Path:
    if transcript_override:
        transcript_path = Path(transcript_override).expanduser().resolve()
        if not transcript_path.exists():
            raise FileNotFoundError(f"Transcript override does not exist: {transcript_path}")
        return transcript_path

    if session_metadata and session_metadata.get("outputs", {}).get("transcript", {}).get("absolutePath"):
        transcript_path = Path(str(session_metadata["outputs"]["transcript"]["absolutePath"]))
        if transcript_path.exists():
            return transcript_path

    expected = TRANSCRIPTS_DIR / f"{session_id}.json"
    if expected.exists():
        return expected

    fallback = newest_file(TRANSCRIPTS_DIR, f"{session_id}*.json") or newest_file(TRANSCRIPTS_DIR, "*.json")
    if fallback:
        return fallback

    raise FileNotFoundError(f"Could not resolve transcript file for session {session_id}.")


def resolve_audio_prof_path(
    session_id: str,
    audio_prof_override: str | None,
    session_metadata: dict[str, Any] | None,
) -> Path | None:
    if audio_prof_override:
        audio_prof_path = Path(audio_prof_override).expanduser().resolve()
        if not audio_prof_path.exists():
            raise FileNotFoundError(f"Audio professionalism override does not exist: {audio_prof_path}")
        return audio_prof_path

    if session_metadata and session_metadata.get("outputs", {}).get("audioProfessionalism", {}).get("absolutePath"):
        audio_prof_path = Path(str(session_metadata["outputs"]["audioProfessionalism"]["absolutePath"]))
        if audio_prof_path.exists():
            return audio_prof_path

    expected = AUDIO_PROF_DIR / f"{session_id}.json"
    if expected.exists():
        return expected

    fallback = newest_file(AUDIO_PROF_DIR, f"{session_id}*.json")
    if fallback:
        return fallback

    return None


def resolve_parsed_rubric_path(parsed_override: str | None) -> Path:
    if parsed_override:
        parsed_path = Path(parsed_override).expanduser().resolve()
        if not parsed_path.exists():
            raise FileNotFoundError(f"Parsed rubric override does not exist: {parsed_path}")
        return parsed_path

    if PARSED_RUBRIC_PATH.exists():
        return PARSED_RUBRIC_PATH

    raise FileNotFoundError(
        "No parsed communication rubric JSON found at "
        f"{PARSED_RUBRIC_PATH}. Run scripts/parse_communication_rubric.py first (the server "
        "does this automatically when the rubric PDF is uploaded)."
    )


def read_parsed_rubric(parsed_rubric_path: Path) -> dict[str, Any]:
    payload = json.loads(parsed_rubric_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Parsed rubric JSON is not an object.")

    criteria = payload.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        raise ValueError("Parsed rubric JSON does not contain any criteria.")

    return payload


def read_generic_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1", errors="ignore")


def build_opensmile_block(audio_prof_payload: dict[str, Any] | None) -> str:
    """Return a deterministic, model-friendly dump of every available openSMILE
    eGeMAPSv02 functional feature for this session.

    The audio professionalism extractor now persists `full_feature_summary` (the
    complete openSMILE row keyed by feature name). For older payloads that only
    stored the abridged set (pitch_mean_semitone, loudness_mean, ...) we fall
    back to a summary view so the prompt is never silently empty.
    """
    if not isinstance(audio_prof_payload, dict):
        return "[opensmile_features_unavailable]"

    audio_features = audio_prof_payload.get("audio_features")
    if not isinstance(audio_features, dict):
        return "[opensmile_features_unavailable]"

    feature_set = str(audio_features.get("feature_set") or "eGeMAPSv02").strip()
    opensmile_version = str(audio_features.get("opensmile_version") or "unknown").strip()

    full_feature_summary = audio_features.get("full_feature_summary")
    if isinstance(full_feature_summary, dict) and full_feature_summary:
        cleaned: dict[str, Any] = {}
        for key in sorted(full_feature_summary.keys()):
            value = full_feature_summary[key]
            if value is None:
                cleaned[str(key)] = None
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                cleaned[str(key)] = value
                continue
            cleaned[str(key)] = round(numeric, 6)

        return json.dumps(
            {
                "feature_set": feature_set,
                "opensmile_version": opensmile_version,
                "feature_count": len(cleaned),
                "features": cleaned,
            },
            ensure_ascii=False,
            indent=2,
        )

    # Fallback: older sessions only stored the summary stats. Still pass them so
    # the model is not blind.
    summary_keys = [
        "pitch_mean_semitone",
        "pitch_std_semitone",
        "pitch_range_semitone",
        "loudness_mean",
        "loudness_std",
        "loudness_range",
        "jitter_local",
        "shimmer_local_db",
        "hnr_db",
    ]
    summary_subset = {key: audio_features.get(key) for key in summary_keys}

    return json.dumps(
        {
            "feature_set": feature_set,
            "opensmile_version": opensmile_version,
            "feature_count": len([v for v in summary_subset.values() if v is not None]),
            "note": (
                "Full openSMILE row not stored in this session. Showing summary stats only."
            ),
            "summary": summary_subset,
        },
        ensure_ascii=False,
        indent=2,
    )


def load_audio_prof_payload(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def format_timestamp_seconds(seconds: float, include_ms: bool = False) -> str:
    safe_seconds = max(0.0, float(seconds or 0.0))
    total_ms = int(round(safe_seconds * 1000))
    hours = total_ms // 3_600_000
    minutes = (total_ms % 3_600_000) // 60_000
    whole_seconds = (total_ms % 60_000) // 1_000
    milliseconds = total_ms % 1_000

    if include_ms:
        return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}"


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
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                text = str(segment.get("text", "")).strip()
                if not text:
                    continue
                speaker = str(segment.get("speaker", "SPEAKER_UNKNOWN")).strip() or "SPEAKER_UNKNOWN"

                start_value = segment.get("start")
                end_value = segment.get("end")
                start_label = str(segment.get("startLabel") or "").strip()
                end_label = str(segment.get("endLabel") or "").strip()

                try:
                    if start_value is not None:
                        start_label = format_timestamp_seconds(float(start_value))
                    if end_value is not None:
                        end_label = format_timestamp_seconds(float(end_value))
                except (TypeError, ValueError):
                    pass

                if start_label and end_label:
                    lines.append(f"[{speaker}] {start_label} - {end_label}: {text}")
                elif start_label:
                    lines.append(f"[{speaker}] {start_label}: {text}")
                else:
                    lines.append(f"[{speaker}]: {text}")

            if lines:
                return "\n".join(lines)

    return json.dumps(parsed, indent=2, ensure_ascii=False)


def clip_text(value: str, max_chars: int, label: str) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text

    kept = text[:max_chars]
    return f"{kept}\n\n[TRUNCATED {label}: original_length={len(text)} chars, kept={max_chars}]"


def resolve_model_candidates() -> list[str]:
    configured_fallbacks = [
        item.strip()
        for item in str(os.getenv("NVIDIA_FALLBACK_MODELS", "") or "").split(",")
        if item.strip()
    ]

    seen: set[str] = set()
    ordered: list[str] = []

    for candidate in [MODEL_NAME, *configured_fallbacks]:
        if candidate in seen:
            continue
        seen.add(candidate)
        ordered.append(candidate)

    return ordered


def format_response_content_for_log(content: str, max_chars: int = 4000) -> str:
    text = str(content or "")
    if not text:
        return "(empty)"
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}... [truncated, total {len(text)} chars]"


def extract_primary_json_dict_from_model_output(raw_text: str) -> dict[str, Any]:
    """Prefer the richest JSON dict that contains `criteria` (mirrors clinical scorer logic)."""

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


def enforce_expected_communication_criteria_or_raise_retry(
    raw_content: str,
    expected_len: int | None,
) -> None:
    """Retry when Nemotron+json_object emits `{}`/`{{\"schema\":\"\"}}`/truncated arrays."""

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


def extract_message_from_response(response: Any) -> Any:
    response_error = getattr(response, "error", None)

    if response_error is None:
        try:
            response_error = response.model_dump().get("error")
        except Exception:
            response_error = None

    if response_error:
        if isinstance(response_error, dict):
            error_message = str(response_error.get("message", "Provider returned error")).strip()
            error_code = str(response_error.get("code", "unknown")).strip()
        else:
            error_message = str(response_error).strip() or "Provider returned error"
            error_code = "unknown"

        raise RuntimeError(f"Model provider error (code={error_code}): {error_message}")

    choices = getattr(response, "choices", None)
    if not choices:
        raise RuntimeError("Model response did not include any choices.")

    first_choice = choices[0]
    message = getattr(first_choice, "message", None)
    if message is None:
        raise RuntimeError("Model response did not include a message payload.")

    finish_reason = str(getattr(first_choice, "finish_reason", "") or "").lower()
    raw_content = str(getattr(message, "content", "") or "").strip()

    if finish_reason == "length" and len(raw_content) < MIN_VALID_CONTENT_CHARS:
        raise RuntimeError(
            "Model response truncated (finish_reason=length) before producing usable JSON. "
            f"This is retryable. response_content={format_response_content_for_log(raw_content)!r}"
        )

    if not raw_content:
        raise RuntimeError(
            f"Model response had empty content (finish_reason={finish_reason or 'unknown'}). "
            f"This is retryable. response_content={format_response_content_for_log(raw_content)!r}"
        )

    if len(raw_content) < MIN_VALID_CONTENT_CHARS:
        raise RuntimeError(
            f"Model response content was suspiciously short ({len(raw_content)} chars, "
            f"finish_reason={finish_reason or 'unknown'}). This is retryable. "
            f"response_content={format_response_content_for_log(raw_content)!r}"
        )

    return message


def is_retryable_model_error(error: Exception) -> bool:
    text = str(error or "").lower()

    code_match = re.search(r"code\s*=\s*(\d{3})", text)
    if code_match:
        try:
            code = int(code_match.group(1))
            if code in RETRYABLE_PROVIDER_ERROR_CODES:
                return True
        except ValueError:
            pass

    retry_tokens = (
        "provider returned error",
        "timeout",
        "timed out",
        "temporarily unavailable",
        "rate limit",
        "rate-limit",
        "rate_limit",
        "too many requests",
        "try again",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
        "model response did not include any choices",
        "truncated",
        "finish_reason=length",
        "empty content",
        "suspiciously short",
        "model response had empty",
        "connection reset",
        "connection error",
        "ssl",
        "read timeout",
        "remote disconnected",
        "missing or non-array field 'criteria'",
        "incomplete criteria array",
        "often a provider-side structured-output scaffolding bug",
    )

    return any(token in text for token in retry_tokens)


def build_attempt_modes_communication() -> list[tuple[str, dict[str, Any]]]:
    json_kwargs: dict[str, Any] = {"response_format": {"type": "json_object"}}

    if NVIDIA_COMMUNICATION_ENABLE_THINKING:
        # Plain text first avoids the provider emitting an empty `{}` / `{\"schema\":\"\"}`
        # JSON scaffold while reasoning is enabled, then escalating to structured JSON fallbacks.
        return [
            ("reasoning+plain_first", {}),
            ("reasoning+json", dict(json_kwargs)),
            (
                "no-reasoning+json",
                {"extra_body": {"reasoning": {"enabled": False}}, **json_kwargs},
            ),
            ("json_only", dict(json_kwargs)),
            ("plain_final", {}),
        ]

    return [
        ("no-reasoning+json", {"extra_body": {"reasoning": {"enabled": False}}, **json_kwargs}),
        ("json_only", dict(json_kwargs)),
        ("plain", {}),
    ]


def create_completion(
    client: OpenAI,
    messages: list[dict[str, Any]],
    *,
    expected_criteria_items: int | None = None,
) -> Any:
    attempt_modes = build_attempt_modes_communication()

    last_error: Exception | None = None
    model_candidates = resolve_model_candidates()

    extra_body: dict[str, Any] = {
        "reasoning_effort": "none",
        "chat_template_kwargs": {"enable_thinking": NVIDIA_COMMUNICATION_ENABLE_THINKING},
    }
    if NVIDIA_COMMUNICATION_ENABLE_THINKING:
        extra_body["reasoning_effort"] = "low"
        extra_body["reasoning_budget"] = NVIDIA_REASONING_BUDGET

    for model_name in model_candidates:
        for mode_name, mode_kwargs in attempt_modes:
            mode_extra_body = mode_kwargs.get("extra_body") if isinstance(mode_kwargs, dict) else None
            merged_extra_body = dict(extra_body)
            if isinstance(mode_extra_body, dict):
                merged_extra_body.update(mode_extra_body)

            mode_kwargs_no_extra: dict[str, Any] = {
                key: value for key, value in mode_kwargs.items() if key != "extra_body"
            }

            for attempt_index in range(1, MAX_COMPLETION_RETRIES_PER_MODE + 1):
                request_payload: dict[str, Any] = {
                    "model": model_name,
                    "messages": messages,
                    "temperature": NVIDIA_TEMPERATURE,
                    "top_p": NVIDIA_TOP_P,
                    "max_tokens": NVIDIA_MAX_TOKENS,
                    "timeout": REQUEST_TIMEOUT_SECONDS,
                    "extra_body": merged_extra_body,
                    **mode_kwargs_no_extra,
                }

                try:
                    response = client.chat.completions.create(**request_payload)
                    message = extract_message_from_response(response)
                    enforce_expected_communication_criteria_or_raise_retry(
                        str(getattr(message, "content", "") or ""),
                        expected_criteria_items,
                    )
                    return message
                except Exception as error:
                    last_error = error
                    print(
                        f"[nvidia_osce_communication] attempt failed model={model_name} mode={mode_name} "
                        f"attempt={attempt_index}/{MAX_COMPLETION_RETRIES_PER_MODE} error={error}",
                        file=sys.stderr,
                    )

                    if not is_retryable_model_error(error):
                        break

                    if attempt_index >= MAX_COMPLETION_RETRIES_PER_MODE:
                        break

                    backoff_seconds = min(8.0, 1.5 * (2 ** (attempt_index - 1)))
                    time.sleep(backoff_seconds)

            continue

    if last_error is None:
        raise RuntimeError("NVIDIA completion failed without a captured error.")

    raise RuntimeError(f"NVIDIA completion failed after retries and fallbacks: {last_error}")


def extract_json_from_text(raw_text: str) -> dict[str, Any]:
    return extract_primary_json_dict_from_model_output(raw_text)


def safe_extract_payload(raw_text: str) -> tuple[dict[str, Any], str | None]:
    try:
        return extract_json_from_text(raw_text), None
    except Exception as error:
        return {}, f"Model JSON parse error: {error}"


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


def normalize_score_label(value: Any) -> str | None:
    token = str(value or "").strip().lower()
    for label in ALLOWED_SCORE_LABELS:
        if token == label.lower():
            return label
    aliases = {
        "0": "None",
        "1": "Some",
        "2": "Most",
        "3": "All",
        "zero": "None",
        "one": "Some",
        "two": "Most",
        "three": "All",
    }
    return aliases.get(token)


def compute_scoring_summary(
    criteria: list[dict[str, Any]],
    max_score: int,
    pass_threshold: int,
) -> dict[str, Any]:
    total_score = sum(int(item.get("points", 0)) for item in criteria)
    average_score = round(total_score / len(criteria), 2) if criteria else 0.0
    label_counts = {label: 0 for label in ALLOWED_SCORE_LABELS}
    for item in criteria:
        label = item.get("score_label")
        if label in label_counts:
            label_counts[label] += 1

    pass_fail = "Pass" if total_score >= pass_threshold else "Fail"
    decision_reason = (
        f"Pass: total {total_score}/{max_score} meets pass threshold of {pass_threshold}."
        if pass_fail == "Pass"
        else f"Fail: total {total_score}/{max_score} is below pass threshold of {pass_threshold}."
    )

    return {
        "total_criteria": len(criteria),
        "total_score": total_score,
        "max_score": max_score,
        "average_score": average_score,
        "pass_threshold": pass_threshold,
        "pass_fail": pass_fail,
        "decision_reason": decision_reason,
        "label_counts": label_counts,
    }


def build_system_prompt(rubric: dict[str, Any]) -> str:
    criteria_view: list[dict[str, Any]] = []
    for criterion in rubric.get("criteria", []):
        criteria_view.append(
            {
                "id": criterion.get("id"),
                "label": criterion.get("label"),
                "section": criterion.get("section"),
                "indicators": criterion.get("indicators", []),
            }
        )

    criteria_text = json.dumps(criteria_view, ensure_ascii=False, indent=2)

    return f"""
You are an OSCE communication assessor. Your job for each rubric criterion is to read the
transcript evidence (and any audio professionalism / OpenSMILE prosody notes) and decide
which of these four qualitative labels best describes how the student performed against
the criterion's performance indicators:

- "All"  -> the student consistently demonstrated ALMOST ALL observable performance indicators
- "Most" -> the student consistently demonstrated MOST of the observable performance indicators
- "Some" -> the student demonstrated ONLY SOME of the observable performance indicators
- "None" -> the student demonstrated NONE of the observable performance indicators

MANDATORY transcript review (complete this BEFORE scoring any criterion):
- Read the ENTIRE transcript from first line to last line at least once before you assign
  the first score. Do not score from memory, assumptions, or only the opening/closing turns.
- For EACH criterion, re-scan the FULL transcript again specifically hunting for evidence.
  Evidence often appears in the middle of the encounter — do not stop after the first match.
- Work chronologically through speaker turns. Note every student turn that could relate to
  the criterion, even if phrasing is informal, fragmented, or mis-transcribed.
- Cross-check consistency: if one part of the transcript supports a higher label but another
  part seems weaker, reconcile using the whole encounter — favour the stronger overall pattern.
- Before finalising JSON, mentally verify every criterion against the complete timeline.
  If you cannot recall where you saw evidence, search the transcript again — do not guess.

CRITICAL reliability context (read carefully):
- The transcript and speaker diarization were produced by an automated speech-to-text
  model. There WILL be transcription errors, dropped words, misheard medical or drug
  names, and ambiguous speaker tags. Interpret garbled lines charitably: if a fragment
  plausibly aligns with the criterion in context, count it as evidence. Do NOT penalise
  the student for transcription artefacts and be CONSISTENT across criteria.
- You MUST take ALL context into account when grading. Missing a line because you skimmed
  the transcript is an assessment error — read thoroughly.

LENIENCY policy (very important — real markers are extremely lenient):
- Be EXTREMELY lenient. Do everything you can to award a good score. Your default posture
  is the highest label you can honestly defend from the transcript.
- Human OSCE markers grade leniently. When evidence is borderline, ALWAYS escalate to the
  next higher label (None -> Some, Some -> Most, Most -> All). Never downgrade on doubt.
- Partial, indirect, implied, or single-turn evidence COUNTS. You do not need verbatim
  phrasing; paraphrasing, tone, context, or a short fragment is enough.
- If even ONE relevant observable indicator is clearly met, prefer at least "Most" or "Some". If
  several are met (even loosely), prefer "All".
- Performance indicators that are peripheral, redundant, overly narrow, or only weakly
  related to the criterion label must NOT drag the score down. You may place them in
  `indicators_not_observable` or treat them as satisfied when the student's overall
  communication on that criterion was clearly adequate — real markers do not fail students
  for missing a minor sub-bullet when the core behaviour was demonstrated.
- When torn between two labels, choose the higher one. Uncertainty is not a reason to score low.
- Use "None" only when, after a full transcript pass, there is genuinely no attempt at that
  communication behaviour anywhere in the encounter.
- Indicators that cannot be observed from a video/audio transcript (eye contact,
  posture, head nods, body language, written notes, gestures, off-camera actions)
  must NOT count against the student. Place them in `indicators_not_observable`
  and base the score only on the indicators that ARE observable.

OpenSMILE / audio professionalism guidance:
- If audio-professionalism JSON is included, you MAY use prosody/talk-time/filler-rate
  hints (words_per_minute, filler_words_per_minute, long pauses, talk-time balance,
  loudness/pitch stats) as soft supporting evidence — especially for criteria about
  vocal delivery, fluency, attentive listening, and turn-taking.
- Treat these features as supplementary; transcript content is still the primary
  source of truth.

Output format:
- Use ONLY {{"All","Most","Some","None"}} as the score_label value.
- Do NOT invent your own numeric scale. The server maps labels to marks afterwards.
- Always cite at least one transcript timestamp (format HH:MM:SS) in `timestamp` as the
  primary evidence moment. Pick the most representative moment.
- Keep the evidence sentence concise and concrete.

Return ONLY valid JSON, no markdown, no commentary.

Required JSON shape:
{{
  "schema": "communication-scoring-v2",
  "session_id": "string",
  "criteria": [
    {{
      "id": 1,
      "label": "criterion label as supplied",
      "score_label": "All|Most|Some|None",
      "indicators_observed": ["indicator phrasing copied or paraphrased from rubric"],
      "indicators_missing": ["..."],
      "indicators_not_observable": ["..."],
      "evidence": "one or two evidence-based sentences (no transcript quotes)",
      "timestamp": "HH:MM:SS"
    }}
  ],
  "overall_summary": "2-4 concise sentences about overall communication quality"
}}

Rubric criteria you MUST score (preserve order, preserve criterion ids exactly):
{criteria_text}
""".strip()


def build_user_prompt(
    session_id: str,
    transcript_path: Path,
    audio_prof_path: Path | None,
    rubric_source_pdf: str | None,
    rubric: dict[str, Any],
    transcript_text: str,
    audio_prof_text: str,
    opensmile_text: str,
) -> str:
    criteria_block = json.dumps(
        [
            {
                "id": criterion.get("id"),
                "label": criterion.get("label"),
                "section": criterion.get("section"),
                "indicators": criterion.get("indicators", []),
            }
            for criterion in rubric.get("criteria", [])
        ],
        ensure_ascii=False,
        indent=2,
    )

    return f"""
Score the OSCE communication rubric for this session.

Session metadata:
- session_id: {session_id}
- transcript_file: {transcript_path}
- audio_professionalism_file: {audio_prof_path or 'missing'}
- rubric_source_pdf: {rubric_source_pdf or 'embedded_parsed_rubric'}

Rubric criteria (structured, preserve order and ids):
---
{criteria_block}
---

Audio professionalism evidence (JSON, derived metrics + speaker stats):
---
{audio_prof_text}
---

OpenSMILE eGeMAPSv02 features (full functional set, one row per session):
- Treat as soft prosody evidence (delivery, fluency, vocal expression).
- Values can be NaN/None if extraction failed for that feature.
---
{opensmile_text}
---

Transcript content (timestamps included):
---
{transcript_text}
---
""".strip()


def validate_output(
    payload: dict[str, Any],
    rubric: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    issues: list[str] = []

    rubric_criteria = rubric.get("criteria", [])
    expected_count = len(rubric_criteria)
    max_score = int(rubric.get("max_score", expected_count * 3))
    pass_threshold = int(rubric.get("pass_threshold", math.ceil(max_score / 2)))

    incoming_criteria = payload.get("criteria")
    if not isinstance(incoming_criteria, list) or not incoming_criteria:
        issues.append("Field 'criteria' must be a non-empty array.")
        incoming_criteria = []

    if len(incoming_criteria) != expected_count:
        issues.append(
            "Criteria count must match the rubric exactly. "
            f"Expected {expected_count}, got {len(incoming_criteria)}."
        )

    normalized_criteria: list[dict[str, Any]] = []
    for index, rubric_item in enumerate(rubric_criteria, start=1):
        source_item = incoming_criteria[index - 1] if index - 1 < len(incoming_criteria) else {}
        if not isinstance(source_item, dict):
            issues.append(f"criteria[{index}] must be an object.")
            source_item = {}

        score_label = normalize_score_label(source_item.get("score_label"))
        if score_label is None:
            issues.append(
                f"criteria[{index}].score_label must be one of {ALLOWED_SCORE_LABELS}."
            )
            score_label = "None"

        evidence = str(source_item.get("evidence", "")).strip()
        if not evidence:
            issues.append(f"criteria[{index}].evidence must contain a sentence.")
            evidence = "Insufficient evidence provided."

        timestamp = normalize_timestamp(source_item.get("timestamp") or source_item.get("evidence_timestamp"))
        if timestamp is None:
            issues.append(f"criteria[{index}].timestamp must be a valid HH:MM:SS string.")
            timestamp = "00:00:00"

        indicators_observed = source_item.get("indicators_observed")
        if not isinstance(indicators_observed, list):
            indicators_observed = []

        indicators_missing = source_item.get("indicators_missing")
        if not isinstance(indicators_missing, list):
            indicators_missing = []

        indicators_not_observable = source_item.get("indicators_not_observable")
        if not isinstance(indicators_not_observable, list):
            indicators_not_observable = []

        normalized_criteria.append(
            {
                "id": int(rubric_item.get("id", index)),
                "label": str(rubric_item.get("label", f"Criterion {index}")).strip()
                or f"Criterion {index}",
                "section": rubric_item.get("section"),
                "indicators_total": rubric_item.get("indicators", []),
                "indicators_observed": [str(item).strip() for item in indicators_observed if str(item).strip()],
                "indicators_missing": [str(item).strip() for item in indicators_missing if str(item).strip()],
                "indicators_not_observable": [
                    str(item).strip() for item in indicators_not_observable if str(item).strip()
                ],
                "score_label": score_label,
                "points": SCORE_LABEL_TO_POINTS[score_label],
                "evidence": evidence,
                "timestamp": timestamp,
            }
        )

    overall_summary = str(payload.get("overall_summary", "")).strip()
    if not overall_summary:
        issues.append("Field 'overall_summary' must be a non-empty string.")
        overall_summary = "Summary unavailable."

    summary = compute_scoring_summary(normalized_criteria, max_score, pass_threshold)

    normalized_payload = {
        "schema": "communication-scoring-v2",
        "session_id": str(payload.get("session_id", "")).strip(),
        "rubric_title": str(rubric.get("title", "Communication Rubric")).strip(),
        "rubric_schema": str(rubric.get("schema", "communication-rubric-v1")).strip(),
        "scoring_scale": SCORE_LABEL_TO_POINTS,
        "criteria": normalized_criteria,
        "overall_summary": overall_summary,
        "scoring_summary": summary,
    }

    return normalized_payload, issues


def build_repair_prompt(issues: list[str], raw_output: str, expected_count: int) -> str:
    issue_text = "\n".join(f"- {issue}" for issue in issues if issue)
    return (
        "Your previous response failed validation. Return corrected JSON only, with no markdown.\n"
        f"You must output exactly {expected_count} criteria items, one per rubric criterion, "
        f"in the same id order.\n"
        f"Every criterion must include a valid score_label from {ALLOWED_SCORE_LABELS}, a "
        "concrete evidence sentence, and a transcript timestamp in HH:MM:SS format.\n"
        "Fix all issues below:\n"
        f"{issue_text}\n\n"
        "Previous invalid output:\n"
        f"{raw_output}"
    )


def main() -> int:
    args = parse_args()
    api_key = require_api_key()

    print(
        f"[nvidia_osce_communication] reasoning="
        f"{'on' if NVIDIA_COMMUNICATION_ENABLE_THINKING else 'off'}"
        f" timeout_s={REQUEST_TIMEOUT_SECONDS}",
        file=sys.stderr,
    )

    session_id = str(args.session_id).strip() if args.session_id else session_id_from_latest_metadata()
    session_metadata = read_session_metadata(session_id)

    transcript_path = resolve_transcript_path(session_id, args.transcript, session_metadata)
    audio_prof_path = resolve_audio_prof_path(session_id, args.audio_professionalism, session_metadata)
    parsed_rubric_path = resolve_parsed_rubric_path(args.parsed_rubric)
    rubric = read_parsed_rubric(parsed_rubric_path)
    communication_criteria_expected = len(rubric.get("criteria", []))

    transcript_text = clip_text(read_json_transcript_text(transcript_path), MAX_TRANSCRIPT_CHARS, "transcript")

    audio_prof_text = "[audio_professionalism_unavailable]"
    audio_prof_payload: dict[str, Any] | None = None
    if audio_prof_path and audio_prof_path.exists():
        audio_prof_text = clip_text(read_generic_text(audio_prof_path), MAX_AUDIO_CONTEXT_CHARS, "audio_professionalism")
        audio_prof_payload = load_audio_prof_payload(audio_prof_path)

    opensmile_text = clip_text(
        build_opensmile_block(audio_prof_payload),
        MAX_OPEN_SMILE_CONTEXT_CHARS,
        "opensmile_features",
    )

    system_prompt = build_system_prompt(rubric)
    user_prompt = build_user_prompt(
        session_id=session_id,
        transcript_path=transcript_path,
        audio_prof_path=audio_prof_path,
        rubric_source_pdf=rubric.get("source_pdf"),
        rubric=rubric,
        transcript_text=transcript_text,
        audio_prof_text=audio_prof_text,
        opensmile_text=opensmile_text,
    )

    base_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    client = OpenAI(base_url=NVIDIA_BASE_URL, api_key=api_key)

    def is_useful_communication_payload(candidate: dict[str, Any], remaining_issues: list[str]) -> bool:
        """The validator falls back to score_label='None', evidence='Insufficient
        evidence provided.', timestamp='00:00:00' when the model returns
        nothing. We refuse to silently ship that as a real result.
        """
        if remaining_issues:
            return False
        criteria_list = candidate.get("criteria") or []
        if not isinstance(criteria_list, list) or not criteria_list:
            return False
        fallback_evidence = "Insufficient evidence provided."
        any_useful = False
        for criterion in criteria_list:
            if not isinstance(criterion, dict):
                continue
            evidence = str(criterion.get("evidence", "")).strip()
            if evidence and evidence != fallback_evidence:
                any_useful = True
                break
        return any_useful

    first_message = create_completion(
        client, base_messages, expected_criteria_items=communication_criteria_expected
    )
    first_payload, parse_error = safe_extract_payload(first_message.content or "")
    normalized_payload, issues = validate_output(first_payload, rubric)
    if parse_error:
        issues = [parse_error, *issues]

    repair_attempts = 0
    max_repair_attempts = 2
    last_message = first_message
    while issues and repair_attempts < max_repair_attempts:
        repair_attempts += 1
        repair_prompt = build_repair_prompt(
            issues,
            last_message.content or "",
            len(rubric.get("criteria", [])),
        )
        follow_up_messages = [
            *base_messages,
            {"role": "assistant", "content": last_message.content or ""},
            {"role": "user", "content": repair_prompt},
        ]
        try:
            next_message = create_completion(
                client, follow_up_messages, expected_criteria_items=communication_criteria_expected
            )
        except Exception as repair_error:
            issues.append(f"Repair attempt {repair_attempts} failed: {repair_error}")
            print(
                f"[nvidia_osce_communication] repair attempt {repair_attempts} aborted: {repair_error}",
                file=sys.stderr,
            )
            break

        last_message = next_message
        next_payload, parse_error = safe_extract_payload(next_message.content or "")
        next_normalized, next_issues = validate_output(next_payload, rubric)
        if parse_error:
            next_issues = [parse_error, *next_issues]
        normalized_payload = next_normalized
        issues = next_issues
        if not issues:
            break

    if issues:
        normalized_payload["warnings"] = issues

    if not is_useful_communication_payload(normalized_payload, issues):
        diagnostic = (
            "NVIDIA communication scoring returned no usable output after all retries "
            "and two repair passes. This usually means the model truncated its response "
            "(finish_reason=length) or returned no JSON. Investigate the model provider "
            "for rate-limit / capacity issues."
        )
        if issues:
            diagnostic += " Validator issues: " + "; ".join(str(item) for item in issues[:5])
        print(f"[nvidia_osce_communication] {diagnostic}", file=sys.stderr)
        raise RuntimeError(diagnostic)

    normalized_payload["session_id"] = session_id
    normalized_payload["transcript_file"] = str(transcript_path)
    normalized_payload["audio_professionalism_file"] = str(audio_prof_path) if audio_prof_path else None
    normalized_payload["rubric_source"] = str(parsed_rubric_path)
    normalized_payload["model"] = MODEL_NAME
    normalized_payload["generated_at"] = datetime.utcnow().isoformat() + "Z"

    output_json = json.dumps(normalized_payload, indent=2, ensure_ascii=False)

    if not args.stdout_only:
        output_path = Path(args.output).expanduser().resolve() if args.output else COMM_SCORES_DIR / f"{session_id}.json"
        write_text_atomic(output_path, output_json + "\n")
        print(f"Saved: {output_path}", file=sys.stderr)

    print(output_json)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
