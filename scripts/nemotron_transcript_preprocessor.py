#!/usr/bin/env python3
"""Nemotron transcript preprocessor — corrects obvious ASR errors in a
diarised OSCE transcript before scoring.

Reads a normalized transcript JSON (whisperx-segments-v1) and sends the model
ONLY a minimal [{id, speaker, text}] projection; timestamps and speaker labels
never travel through the LLM. Writes:

    {"schema": "llm-preprocess-v1", "model": "<model>",
     "segments": [{"id": ..., "text": "..."}]}

The backend (app/pipeline/llm_preprocess.py) merges the corrected texts back
by id. A response is only accepted when it returns every input id exactly
once, so a hallucinated/truncated reply triggers a retry instead of a merge.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from env_loader import load_env_file
from llm_bootstrap import (
    ChatResponse,
    LLMRouter,
    build_chat_request,
    build_router_from_env,
    describe_routing,
    read_float_env,
    read_int_env,
    validator_from,
)
from scorer_env_allowlist import NEMOTRON_PREPROCESSOR_ENV_ALLOWLIST

ROOT_DIR = Path(__file__).resolve().parents[1]
load_env_file(ROOT_DIR, allowed_keys=NEMOTRON_PREPROCESSOR_ENV_ALLOWLIST)

# Provider and model come from the settings-driven router, the same selection
# the scorers use. See scripts/llm_bootstrap.py.


REQUEST_TIMEOUT_SECONDS = read_int_env("NVIDIA_REQUEST_TIMEOUT_SECONDS", 360)
# Retry count and backoff are the router's policy (LLM_MAX_ATTEMPTS_PER_MODE),
# shared with the scorers so all three behave the same under a rate limit.

# Low temperature: this is a constrained editing task, not generation.
TEMPERATURE = read_float_env("NVIDIA_PREPROCESS_TEMPERATURE", 0.1)
MAX_TOKENS = read_int_env("NVIDIA_PREPROCESS_MAX_TOKENS", 24_576)
# ponytail: single LLM call per transcript; inputs beyond this cap fail fast
# with a clear message. Upgrade path: chunk segments into batches with
# per-batch id validation.
MAX_INPUT_CHARS = read_int_env("NVIDIA_PREPROCESS_MAX_INPUT_CHARS", 120_000)

SYSTEM_PROMPT = """You are a professional medical transcription editor. You will receive a JSON array of dialogue segments from a speaker-diarised automatic transcription of an OSCE (clinical examination roleplay) between a student clinician and an actor-patient. Each segment has an `id`, a `speaker` label, and `text`.

Your task: correct obvious automatic-transcription errors only — misheard words, garbled medical terminology (e.g. "parasympamol" -> "paracetamol", "block nurse" -> "blocked nose"), and grammatical breaks caused by mis-transcription. Use the clinical context of the whole dialogue to resolve ambiguous words.

Strict rules:
1. Preserve meaning and tone. Never paraphrase, summarise, or "improve" phrasing that is already plausible speech. Disfluencies ("um", "uh", repetitions) are authentic speech — keep them.
2. Never merge, split, reorder, add, or remove segments. Return every input `id` exactly once, with only its corrected `text`.
3. If a segment needs no correction, return its text unchanged.
4. Output ONLY a JSON object of the form {"segments": [{"id": <id>, "text": "<corrected text>"}]} — no explanations, no markdown fences."""


def extract_json_object(raw_text: str) -> dict[str, Any]:
    text = str(raw_text or "").strip()
    if not text:
        raise ValueError("Model returned empty content.")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace < 0 or last_brace <= first_brace:
        raise ValueError("Model output did not contain a JSON object.")
    parsed = json.loads(text[first_brace : last_brace + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Parsed JSON was not an object.")
    return parsed


def validate_segments(payload: dict[str, Any], expected_ids: list[Any]) -> list[dict[str, Any]]:
    """Enforce the every-id-exactly-once contract; raise ValueError otherwise
    so the caller retries instead of merging a partial/hallucinated reply."""
    segments = payload.get("segments")
    if not isinstance(segments, list):
        raise ValueError("Model JSON had missing or non-array field 'segments'.")
    returned: dict[str, str] = {}
    for item in segments:
        if not isinstance(item, dict) or item.get("id") is None:
            raise ValueError("Model JSON contained a segment without an id.")
        key = str(item["id"])
        if key in returned:
            raise ValueError(f"Model JSON returned segment id {key} more than once.")
        returned[key] = str(item.get("text") or "")
    expected = {str(identifier) for identifier in expected_ids}
    if set(returned) != expected:
        missing = sorted(expected - set(returned))[:5]
        extra = sorted(set(returned) - expected)[:5]
        raise ValueError(f"Model JSON id mismatch (missing={missing}, extra={extra}).")
    # Preserve the input order for a deterministic output artifact.
    return [{"id": identifier, "text": returned[str(identifier)]} for identifier in expected_ids]


def request_corrections(router: LLMRouter, items: list[dict[str, Any]]) -> ChatResponse:
    """Ask the configured model to correct every segment, and insist it did.

    The id check is the whole safety property of this step: a reply that drops
    or invents ids would merge one segment's correction onto another's
    timestamps. Raising from the validator makes that a retry inside the
    router — across fallback providers too — rather than a merge of bad data.
    """
    user_prompt = (
        "Correct the following transcription segments. Remember: return every id exactly once.\n\n"
        + json.dumps({"segments": items}, ensure_ascii=False)
    )
    expected_ids = [item["id"] for item in items]

    def check(content: str) -> None:
        validate_segments(extract_json_object(content), expected_ids)

    request = build_chat_request(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        label="transcript-preprocess",
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
        timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        # A single corrected word is a legitimate reply for a one-segment
        # transcript, so the scorers' 40-character floor does not apply here.
        min_content_chars=1,
    )
    return router.complete(request, validate=validator_from(check))


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def self_check() -> None:
    payload = {"segments": [{"id": 2, "text": "b"}, {"id": 1, "text": "a"}]}
    assert validate_segments(payload, [1, 2]) == [{"id": 1, "text": "a"}, {"id": 2, "text": "b"}]
    for bad in (
        {"segments": [{"id": 1, "text": "a"}]},  # missing id 2
        {"segments": [{"id": 1, "text": "a"}, {"id": 1, "text": "b"}]},  # duplicate
        {"segments": [{"id": 1, "text": "a"}, {"id": 3, "text": "c"}]},  # extra id
        {"segments": "nope"},
    ):
        try:
            validate_segments(bad, [1, 2])
        except ValueError:
            pass
        else:
            raise AssertionError(f"validate_segments accepted invalid payload: {bad}")
    assert extract_json_object('noise {"a": 1} trailing') == {"a": 1}
    print("self-check ok")


def main() -> int:
    parser = argparse.ArgumentParser(description="Correct ASR errors in a normalized OSCE transcript via Nemotron.")
    parser.add_argument("--transcript", help="Path to the normalized transcript JSON (whisperx-segments-v1).")
    parser.add_argument("--output", help="Path to write the llm-preprocess-v1 JSON output.")
    parser.add_argument("--self-check", action="store_true", help="Run the pure-logic self test and exit.")
    args = parser.parse_args()

    if args.self_check:
        self_check()
        return 0
    if not args.transcript or not args.output:
        parser.error("--transcript and --output are required")

    transcript = json.loads(Path(args.transcript).read_text(encoding="utf-8"))
    segments = transcript.get("segments") or []
    items = [
        {"id": segment.get("id"), "speaker": segment.get("speaker"), "text": str(segment.get("text") or "").strip()}
        for segment in segments
        if segment.get("id") is not None and str(segment.get("text") or "").strip()
    ]
    output_path = Path(args.output)
    if not items:
        write_json_atomic(output_path, {"schema": "llm-preprocess-v1", "model": "", "segments": []})
        print("No non-empty segments to preprocess; wrote empty output.")
        return 0

    serialized_size = len(json.dumps({"segments": items}, ensure_ascii=False))
    if serialized_size > MAX_INPUT_CHARS:
        raise RuntimeError(
            f"Transcript too large for a single LLM preprocess call "
            f"({serialized_size} chars > {MAX_INPUT_CHARS}). Raise NVIDIA_PREPROCESS_MAX_INPUT_CHARS "
            "or disable LLM preprocess for this run."
        )

    router = build_router_from_env()
    print(f"Preprocessing {len(items)} segment(s) with {describe_routing(router)}...")
    response = request_corrections(router, items)
    corrected = validate_segments(extract_json_object(response.content), [item["id"] for item in items])
    write_json_atomic(
        output_path,
        {"schema": "llm-preprocess-v1", "model": response.model, "segments": corrected},
    )
    changed = sum(
        1 for item, original in zip(corrected, items) if str(item["text"]).strip() != str(original["text"]).strip()
    )
    print(f"Done: {changed} of {len(items)} segment(s) changed. Output: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
