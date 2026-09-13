#!/usr/bin/env python3
"""Content scorer: one model marks one consultation against its case-study rubric.

The prompt, the rubric extraction and the sheet validator live in
``content_marking.py`` and are shared with every other content marker; this
script owns the model call, the crash checkpoint and the repair loop.
"""
from __future__ import annotations

import argparse
import json
import sys
from functools import partial
from pathlib import Path
from typing import Any

from content_marking import (
    MAX_CASE_STUDY_CONTEXT_CHARS,
    MAX_RUBRIC_SECTION_CHARS,
    MAX_TRANSCRIPT_CHARS,
    MIN_VALID_CONTENT_CHARS,
    PROMPT_VERSION,
    build_follow_up_messages,
    build_system_prompt,
    build_user_prompt,
    compute_scoring_summary,
    read_file_as_context_text,
    to_repo_relative,
    validate_output,
)
from case_study_rubric import load_case_study_rubric
from env_loader import load_env_file
from llm_bootstrap import (
    build_router_from_env,
    clip_text,
    describe_routing,
    safe_extract_payload,
    write_text_atomic,
)
from llm_bootstrap import (
    create_completion as complete_scoring,
)
from llm_bootstrap import (
    enforce_expected_criteria_array as enforce_expected_criteria_array_or_raise_retry,
)
from scorer_checkpoint import (
    CheckpointMessage,
    checkpoint_file_signature,
    checkpoint_matches,
    checkpoint_path_for_output,
    read_checkpoint,
    write_checkpoint,
)
from scorer_inputs import optional_directory, required_file, run_main, session_id_from

ROOT_DIR = Path(__file__).resolve().parents[1]
load_env_file(ROOT_DIR)
STORAGE_DIR = ROOT_DIR / "storage"
SCORES_OUTPUT_DIR = STORAGE_DIR / "output" / "scores"

# Which provider and model run is no longer decided here. The API resolves the
# operator's primary/fallback choice from the settings database and passes it in
# OSCE_LLM_ROUTING; running this script by hand with no routing variable falls
# back to the legacy NVIDIA_MODEL_NAME / NVIDIA_FALLBACK_MODELS behaviour. See
# scripts/llm_bootstrap.py and fastapi_backend/app/llm/.

CHECKPOINT_SCHEMA = "nvidia-osce-assessor-checkpoint-v1"
checkpoint_matches = partial(checkpoint_matches, schema=CHECKPOINT_SCHEMA)
write_checkpoint = partial(write_checkpoint, schema=CHECKPOINT_SCHEMA)


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
    parser.add_argument("--session-id", help="Session ID (defaults to the transcript file's stem)")
    parser.add_argument(
        "--transcript",
        required=True,
        help="Normalised transcript JSON (storage/output/transcripts/<session-id>.json). Required.",
    )
    parser.add_argument(
        "--case-study",
        required=True,
        help="This session's case-study PDF (the rubric is embedded in it). Required.",
    )
    parser.add_argument(
        "--rubric-cache",
        help=(
            "Directory holding extracted case-study rubrics, shared by every marker and the "
            "adjudicator. Optional: without it the rubric is parsed from the PDF in-process."
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



create_completion = partial(
    complete_scoring,
    label="content-scoring",
    criteria_validator=enforce_expected_criteria_array_or_raise_retry,
    min_content_chars=MIN_VALID_CONTENT_CHARS,
)



def main() -> int:
    args = parse_args()
    router = build_router_from_env()
    routing_summary = describe_routing(router)
    print(f"[nvidia_osce_assessor] LLM routing: {routing_summary}", file=sys.stderr)

    # Both inputs are handed over by the API (or the operator); nothing is
    # searched for. See scripts/scorer_inputs.py for why.
    transcript_path = required_file(args.transcript, flag="--transcript", label="Transcript")
    case_study_path = required_file(args.case_study, flag="--case-study", label="Case-study PDF")
    session_id = session_id_from(args, transcript_path)
    output_path = None if args.stdout_only else (
        Path(args.output).expanduser().resolve() if args.output else SCORES_OUTPUT_DIR / f"{session_id}.json"
    )
    checkpoint_path = checkpoint_path_for_output(output_path) if output_path is not None else None

    transcript_text = clip_text(read_file_as_context_text(transcript_path), MAX_TRANSCRIPT_CHARS, "transcript")

    # Parsing the rubric out of the PDF is a pure function of its bytes, so a
    # panel's markers share one extraction instead of each running their own.
    case_study_rubric = load_case_study_rubric(
        case_study_path, cache_dir=optional_directory(args.rubric_cache)
    )
    print(
        f"[nvidia_osce_assessor] case-study rubric: {len(case_study_rubric.criteria)} criteria "
        f"({case_study_rubric.source})",
        file=sys.stderr,
    )
    case_study_context_text = case_study_rubric.context_text
    rubric_section_text = case_study_rubric.rubric_section_text
    rubric_criteria = case_study_rubric.criteria

    checkpoint_context = {
        "session_id": session_id,
        # Part of the checkpoint fingerprint: changing the configured model
        # must invalidate a half-finished run rather than silently splicing
        # one model's output into another's.
        "model": routing_summary,
        "transcript": checkpoint_file_signature(transcript_path),
        "case_study": checkpoint_file_signature(case_study_path),
        "rubric_criteria_count": len(rubric_criteria),
        "prompt_version": PROMPT_VERSION,
    }
    checkpoint_payload = read_checkpoint(checkpoint_path)
    checkpoint_state = (
        checkpoint_payload.get("state")
        if checkpoint_matches(checkpoint_payload, checkpoint_context)
        and isinstance(checkpoint_payload.get("state"), dict)
        else None
    )

    case_study_context_text = clip_text(
        case_study_context_text,
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
        first_message = create_completion(router, base_messages, expected_items=len(rubric_criteria))
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
            next_message = create_completion(router, follow_up_messages, expected_items=len(rubric_criteria))
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
    # Which wording produced these marks. Sheets from different prompt versions
    # are not comparable, and a panel's agreement figures assume they are.
    normalized_payload["prompt_version"] = PROMPT_VERSION
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
    run_main(main, script_name="nvidia_osce_assessor")
