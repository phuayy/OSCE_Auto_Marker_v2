#!/usr/bin/env python3
"""Panel adjudicator: turns several markers' sheets into one final sheet.

The markers have already run (``nvidia_osce_assessor.py``, once per model) and
each wrote a full sheet. This script:

1. lines the sheets up against the rubric and settles, in code, every
   criterion the markers agree on;
2. asks the adjudicating model — once, in a single batched request — about
   the criteria they disagree on, showing it each marker's position and the
   transcript around the moments they cited;
3. merges the markers' Keep/Start/Stop feedback into one, consistent with the
   final verdicts;
4. writes the final sheet in the assessor's own schema plus a ``panel`` block
   that records every vote, and a separate adjudication record with the
   prompts' inputs and the raw replies.

The adjudicator never re-marks the sheet. When it cannot answer at all — no
target, every attempt failed, or the run was started ``--without-adjudicator``
— each disputed criterion falls to the tie-break policy and is labelled as
such, so the final sheet always says how each mark was decided.

Prompts and validators live in ``content_marking.py`` beside the markers' own,
so the adjudicator is held to the same leniency standard by construction; the
reconciliation rules live in ``app.pipeline.marking.reconciliation`` so the
API can assemble a degraded (single-survivor) sheet with the same vocabulary.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from content_marking import (
    ADJUDICATION_MIN_VALID_CONTENT_CHARS,
    ADJUDICATION_PROMPT_VERSION,
    SIDED_WITH_NEITHER,
    build_adjudication_messages,
    build_feedback_merge_messages,
    build_follow_up_messages,
    compute_scoring_summary,
    enforce_expected_resolutions_array,
    marker_letter,
    read_file_as_context_text,
    to_repo_relative,
    validate_adjudication_output,
    validate_feedback_output,
    validate_output,
)
from case_study_rubric import load_case_study_rubric
from env_loader import load_env_file
from llm_bootstrap import (
    MIN_PANEL_MARKERS,
    LLMRouter,
    TieBreak,
    build_router_from_env,
    describe_routing,
    reconciliation,
    safe_extract_payload,
    write_text_atomic,
)
from llm_bootstrap import (
    create_completion as complete_scoring,
)
from scorer_checkpoint import (
    CheckpointMessage,
    checkpoint_file_signature,
    checkpoint_matches,
    checkpoint_path_for_output,
    read_checkpoint,
    write_checkpoint,
)
from scorer_inputs import InputError, optional_directory, required_file, run_main, session_id_from

ROOT_DIR = Path(__file__).resolve().parents[1]
load_env_file(ROOT_DIR)

CHECKPOINT_SCHEMA = "osce-panel-adjudicator-checkpoint-v1"
# Schema of the adjudication record written beside the marker sheets.
ADJUDICATION_RECORD_SCHEMA = "content-adjudication-record-v1"
# Make at most TWO repair attempts per call, as the assessor does.
MAX_REPAIR_ATTEMPTS = 2

checkpoint_matches = partial(checkpoint_matches, schema=CHECKPOINT_SCHEMA)
write_checkpoint = partial(write_checkpoint, schema=CHECKPOINT_SCHEMA)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reconcile several content-marker sheets into one final sheet, adjudicating disagreements.",
    )
    parser.add_argument("--session-id", help="Session ID (defaults to the transcript file's stem)")
    parser.add_argument("--transcript", required=True, help="Normalised transcript JSON. Required.")
    parser.add_argument("--case-study", required=True, help="This session's case-study PDF. Required.")
    parser.add_argument(
        "--marker",
        action="append",
        default=[],
        metavar="PATH",
        help="A marker's sheet (repeat once per marker; at least two). The file stem is the marker key.",
    )
    parser.add_argument(
        "--tie-break",
        default=str(TieBreak.LENIENT),
        choices=[str(policy) for policy in TieBreak],
        help="What settles a disputed criterion when the adjudicator cannot.",
    )
    parser.add_argument(
        "--rubric-cache",
        help=(
            "Directory holding extracted case-study rubrics, shared with the markers. Optional: "
            "without it the rubric is parsed from the PDF in-process."
        ),
    )
    parser.add_argument("--output", required=True, help="Final sheet path (storage/output/scores/<id>.json).")
    parser.add_argument(
        "--adjudication-output",
        help="Adjudication record path (defaults to adjudication.json beside the first marker sheet).",
    )
    parser.add_argument(
        "--without-adjudicator",
        action="store_true",
        help="Call no model: settle every disputed criterion by the tie-break policy.",
    )
    parser.add_argument(
        "--warning",
        action="append",
        default=[],
        help="A note to record on the panel block (e.g. a marker the caller could not run). Repeatable.",
    )
    return parser.parse_args(argv)


# --- inputs ------------------------------------------------------------------


def load_rubric_criteria(case_study_path: Path, cache_dir: Path | None = None) -> list[dict[str, Any]]:
    """The same extraction the markers ran, so the sheets align to it.

    Literally the same: both go through ``case_study_rubric``, so when the
    markers have already parsed this PDF the adjudicator adopts their result
    rather than repeating the parse — and the two can never diverge.
    """
    return load_case_study_rubric(case_study_path, cache_dir=cache_dir).criteria


def load_transcript(transcript_path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(transcript_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"Transcript {transcript_path} is not readable JSON: {error}") from error
    return parsed if isinstance(parsed, dict) else {}


def load_marker_sheet(
    path: Path,
    rubric_criteria: list[dict[str, Any]],
) -> tuple[reconciliation.MarkerSheet, dict[str, Any]]:
    """A marker's sheet, re-validated against the rubric.

    The API only hands over sheets that passed its own reuse predicate, but the
    validator is cheap and this is the boundary where a sheet from another
    rubric would otherwise be reconciled item-by-item against the wrong
    criteria.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"Marker sheet {path} is not readable JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"Marker sheet {path} is not a JSON object.")
    raw_labels = [
        str(item.get("label") or "")
        for item in (payload.get("criteria") if isinstance(payload.get("criteria"), list) else [])
        if isinstance(item, dict)
    ]
    reconciliation.check_sheet_labels(path.stem, [str(item["label"]) for item in rubric_criteria], raw_labels)
    normalised, issues = validate_output(payload, rubric_criteria)
    if issues:
        raise ValueError(f"Marker sheet {path} fails validation: " + "; ".join(issues[:5]))
    # Provenance and the summary are outside the validator's remit; carry them
    # from the raw sheet.
    for key in ("model", "model_provider", "prompt_version", "scoring_summary", "warnings"):
        if key in payload:
            normalised[key] = payload[key]
    return reconciliation.MarkerSheet.from_payload(path.stem, normalised), normalised


# --- adjudication ------------------------------------------------------------


@dataclass
class CallOutcome:
    """What one adjudicator call produced, for the record and the final sheet."""

    called: bool = False
    ok: bool = False
    error: str = ""
    model: str = ""
    provider_id: str = ""
    repair_attempts: int = 0
    issues: list[str] = field(default_factory=list)
    raw_content: str = ""

    def describe(self) -> dict[str, Any]:
        return {
            "called": self.called,
            "ok": self.ok,
            "error": self.error,
            "model": self.model,
            "provider_id": self.provider_id,
            "repair_attempts": self.repair_attempts,
            "issues": list(self.issues),
        }


def build_dispute_requests(
    session_id: str,
    disputes: tuple[reconciliation.AlignedCriterion, ...],
    sheets: list[reconciliation.MarkerSheet],
    transcript: dict[str, Any],
) -> list[dict[str, Any]]:
    """What the adjudicator is shown per dispute, with marker order shuffled.

    ``order`` records which sheet each lettered position came from, so the
    reply's ``sided_with`` letter can be mapped back to a marker.
    """
    requests: list[dict[str, Any]] = []
    for criterion in disputes:
        order = reconciliation.shuffled_order(f"{session_id}:{criterion.index}", len(sheets))
        positions: list[dict[str, Any]] = []
        for letter_index, sheet_position in enumerate(order):
            vote = criterion.votes[sheet_position]
            positions.append(
                {
                    "letter": marker_letter(letter_index),
                    "marker_key": sheets[sheet_position].key,
                    "value": vote.value,
                    "timestamp": vote.timestamp if vote.cites_evidence else "",
                    "reason": vote.reason,
                    "evidence": reconciliation.transcript_window(transcript, vote.timestamp)
                    if vote.cites_evidence
                    else "",
                }
            )
        requests.append(
            {
                # 1-based in the prompt: examiners count criteria from one.
                "index": criterion.index + 1,
                "label": criterion.label,
                "is_critical": criterion.is_critical,
                "order": list(order),
                "positions": positions,
            }
        )
    return requests


def _completion_with_repairs(
    router: LLMRouter,
    base_messages: list[dict[str, str]],
    *,
    label: str,
    expected_items: int | None,
    criteria_validator,
    min_content_chars: int,
    validate,
    checkpoint_path: Path | None,
    checkpoint_context: dict[str, Any],
    checkpoint_key: str,
    checkpoint_state: dict[str, Any],
) -> tuple[Any, list[str], CallOutcome]:
    """One call plus up to ``MAX_REPAIR_ATTEMPTS`` structured re-prompts.

    ``validate`` maps a parsed payload to ``(result, issues)``; the loop stops
    at the first reply with no issues. Each reply is checkpointed under
    ``checkpoint_key`` so a crash between calls resumes from the last reply
    rather than paying for it again.
    """
    outcome = CallOutcome(called=True)
    saved = checkpoint_state.get(checkpoint_key) if isinstance(checkpoint_state.get(checkpoint_key), dict) else None

    def remember(message: Any, attempts: int) -> None:
        checkpoint_state[checkpoint_key] = {
            "content": message.content or "",
            "model": getattr(message, "model", ""),
            "provider_id": getattr(message, "provider_id", ""),
            "repair_attempts": attempts,
        }
        write_checkpoint(checkpoint_path, checkpoint_context, checkpoint_state)

    if saved and saved.get("content"):
        last_message: Any = CheckpointMessage(
            str(saved["content"]), model=str(saved.get("model") or ""), provider_id=str(saved.get("provider_id") or "")
        )
        repair_attempts = int(saved.get("repair_attempts") or 0)
    else:
        try:
            last_message = complete_scoring(
                router,
                base_messages,
                label=label,
                expected_items=expected_items,
                criteria_validator=criteria_validator,
                min_content_chars=min_content_chars,
            )
        except Exception as error:  # noqa: BLE001 - reported on the record, decided by the caller
            outcome.error = str(error) or type(error).__name__
            return None, [outcome.error], outcome
        repair_attempts = 0
        remember(last_message, repair_attempts)

    payload, parse_error = safe_extract_payload(last_message.content or "")
    result, issues = validate(payload)
    if parse_error:
        issues = [parse_error, *issues]

    while issues and repair_attempts < MAX_REPAIR_ATTEMPTS:
        repair_attempts += 1
        follow_up = build_follow_up_messages(base_messages, last_message, issues)
        try:
            next_message = complete_scoring(
                router,
                follow_up,
                label=label,
                expected_items=expected_items,
                criteria_validator=criteria_validator,
                min_content_chars=min_content_chars,
            )
        except Exception as error:  # noqa: BLE001
            issues.append(f"Repair attempt {repair_attempts} failed: {error}")
            print(f"[osce_panel_adjudicator] {label} repair attempt {repair_attempts} aborted: {error}", file=sys.stderr)
            break
        last_message = next_message
        remember(last_message, repair_attempts)
        payload, parse_error = safe_extract_payload(last_message.content or "")
        result, issues = validate(payload)
        if parse_error:
            issues = [parse_error, *issues]

    outcome.model = getattr(last_message, "model", "") or ""
    outcome.provider_id = getattr(last_message, "provider_id", "") or ""
    outcome.repair_attempts = repair_attempts
    outcome.issues = list(issues)
    outcome.raw_content = last_message.content or ""
    outcome.ok = not issues
    return result, issues, outcome


def adjudicate(
    router: LLMRouter | None,
    session_id: str,
    requests: list[dict[str, Any]],
    disputes: tuple[reconciliation.AlignedCriterion, ...],
    tie_break: TieBreak,
    *,
    checkpoint_path: Path | None = None,
    checkpoint_context: dict[str, Any] | None = None,
    checkpoint_state: dict[str, Any] | None = None,
) -> tuple[dict[int, reconciliation.Resolution], CallOutcome]:
    """Resolutions for every dispute, from the adjudicator where it answered
    and from the tie-break where it did not."""
    by_index = {criterion.index: criterion for criterion in disputes}
    resolutions: dict[int, reconciliation.Resolution] = {}
    outcome = CallOutcome()

    if router is not None and requests:
        result, _issues, outcome = _completion_with_repairs(
            router,
            build_adjudication_messages(session_id, requests),
            label="panel-adjudication",
            expected_items=len(requests),
            criteria_validator=enforce_expected_resolutions_array,
            min_content_chars=ADJUDICATION_MIN_VALID_CONTENT_CHARS,
            validate=lambda payload: validate_adjudication_output(payload, requests),
            checkpoint_path=checkpoint_path,
            checkpoint_context=checkpoint_context or {},
            checkpoint_key="adjudication",
            checkpoint_state=checkpoint_state if checkpoint_state is not None else {},
        )
        for request in requests:
            answer = (result or {}).get(int(request["index"]))
            if not answer:
                continue
            index = int(request["index"]) - 1
            sided_with: int | None = None
            letter = answer.get("sided_with")
            if letter and letter != SIDED_WITH_NEITHER:
                position = next(
                    (item for item in request["positions"] if item["letter"] == letter), None
                )
                if position is not None:
                    sided_with = request["order"][request["positions"].index(position)]
            resolutions[index] = reconciliation.Resolution(
                index=index,
                value=answer["value"],
                resolution=reconciliation.RESOLUTION_ADJUDICATED,
                timestamp=answer["timestamp"],
                reason=answer["reason"],
                sided_with=sided_with,
                confidence=answer.get("confidence"),
            )

    # Whatever the adjudicator did not settle — nothing, if it was not called
    # or failed outright; a few criteria, if its reply was partly unusable —
    # falls to the policy, labelled per criterion so the sheet says so.
    for index, criterion in by_index.items():
        if index not in resolutions:
            resolutions[index] = reconciliation.tie_break_resolution_for(criterion, tie_break)
    return resolutions, outcome


def merge_feedback(
    router: LLMRouter | None,
    session_id: str,
    final_criteria: list[dict[str, Any]],
    sheets: list[reconciliation.MarkerSheet],
    *,
    checkpoint_path: Path | None = None,
    checkpoint_context: dict[str, Any] | None = None,
    checkpoint_state: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], CallOutcome, str]:
    """One Keep/Start/Stop block and summary for the student.

    Merged by the adjudicator when it can be; otherwise taken from the marker
    whose verdicts sit closest to the final ones — the least wrong feedback to
    hand over, and labelled as that marker's on the record.
    """
    outcome = CallOutcome()
    if router is not None:
        blocks = [
            {
                "letter": marker_letter(position),
                "keep_start_stop": dict(sheet.keep_start_stop),
                "overall_summary": sheet.overall_summary,
            }
            for position, sheet in enumerate(sheets)
        ]
        result, _issues, outcome = _completion_with_repairs(
            router,
            build_feedback_merge_messages(session_id, final_criteria, blocks),
            label="panel-feedback",
            expected_items=None,
            criteria_validator=lambda _content, _expected: None,
            min_content_chars=ADJUDICATION_MIN_VALID_CONTENT_CHARS,
            validate=validate_feedback_output,
            checkpoint_path=checkpoint_path,
            checkpoint_context=checkpoint_context or {},
            checkpoint_key="feedback",
            checkpoint_state=checkpoint_state if checkpoint_state is not None else {},
        )
        if outcome.ok and result:
            return result, outcome, "merged"

    closest = reconciliation.closest_sheet_index([item["value"] for item in final_criteria], sheets)
    sheet = sheets[closest]
    return (
        {"keep_start_stop": dict(sheet.keep_start_stop), "overall_summary": sheet.overall_summary},
        outcome,
        f"marker:{sheet.key}",
    )


# --- the run -----------------------------------------------------------------


def prompt_fingerprint(requests: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(requests, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def run_panel(args: argparse.Namespace, router: LLMRouter | None) -> int:
    transcript_path = required_file(args.transcript, flag="--transcript", label="Transcript")
    case_study_path = required_file(args.case_study, flag="--case-study", label="Case-study PDF")
    marker_paths = [required_file(item, flag="--marker", label="Marker sheet") for item in args.marker]
    if len(marker_paths) < MIN_PANEL_MARKERS:
        raise InputError(f"A panel needs at least {MIN_PANEL_MARKERS} marker sheets: pass --marker <path> per marker.")
    session_id = session_id_from(args, transcript_path)
    output_path = Path(args.output).expanduser().resolve()
    adjudication_path = (
        Path(args.adjudication_output).expanduser().resolve()
        if args.adjudication_output
        else marker_paths[0].with_name("adjudication.json")
    )
    tie_break = TieBreak(str(args.tie_break))
    configured_targets = router.resolved_targets() if router is not None else ()
    configured = configured_targets[0] if configured_targets else None
    routing_summary = describe_routing(router) if router is not None else "(no adjudicator)"
    print(f"[osce_panel_adjudicator] adjudicator routing: {routing_summary}", file=sys.stderr)

    rubric_criteria = load_rubric_criteria(case_study_path, optional_directory(args.rubric_cache))
    transcript = load_transcript(transcript_path)
    loaded = [load_marker_sheet(path, rubric_criteria) for path in marker_paths]
    sheets = [sheet for sheet, _payload in loaded]

    aligned = reconciliation.align_sheets(sheets, rubric_criteria)
    reconciled = reconciliation.reconcile(aligned)
    agreement = reconciliation.agreement_summary(reconciled, sheets)
    requests = build_dispute_requests(session_id, reconciled.disputes, sheets, transcript)
    print(
        f"[osce_panel_adjudicator] {agreement['agreed']}/{agreement['total']} criteria agreed; "
        f"{agreement['disputed']} disputed",
        file=sys.stderr,
    )

    checkpoint_path = checkpoint_path_for_output(output_path)
    checkpoint_context = {
        "session_id": session_id,
        "routing": routing_summary,
        "transcript": checkpoint_file_signature(transcript_path),
        "case_study": checkpoint_file_signature(case_study_path),
        "markers": [checkpoint_file_signature(path) for path in marker_paths],
        "tie_break": str(tie_break),
        "prompt_version": ADJUDICATION_PROMPT_VERSION,
        "disputes": prompt_fingerprint(requests),
    }
    checkpoint_payload = read_checkpoint(checkpoint_path)
    checkpoint_state: dict[str, Any] = (
        dict(checkpoint_payload.get("state") or {})
        if checkpoint_matches(checkpoint_payload, checkpoint_context)
        and isinstance(checkpoint_payload.get("state"), dict)
        else {}
    )

    resolutions, adjudication = adjudicate(
        router,
        session_id,
        requests,
        reconciled.disputes,
        tie_break,
        checkpoint_path=checkpoint_path,
        checkpoint_context=checkpoint_context,
        checkpoint_state=checkpoint_state,
    )
    final_criteria = reconciliation.build_final_criteria(aligned, resolutions)
    feedback, feedback_outcome, feedback_source = merge_feedback(
        router,
        session_id,
        final_criteria,
        sheets,
        checkpoint_path=checkpoint_path,
        checkpoint_context=checkpoint_context,
        checkpoint_state=checkpoint_state,
    )

    warnings = list(args.warning)
    if reconciled.disputes and not adjudication.ok:
        warnings.append(
            "The adjudicator did not settle every disputed criterion; "
            f"the remainder fell to the '{tie_break}' tie-break."
            + (f" ({adjudication.error})" if adjudication.error else "")
        )
    if feedback_source != "merged":
        warnings.append(f"Coaching feedback is one marker's ({feedback_source}); it could not be merged.")

    # Provenance names the model that actually answered; when none did, the
    # one that was configured to, so the sheet still says who was asked.
    adjudicator_provider = adjudication.provider_id or (configured.provider_id if configured else "")
    adjudicator_model = adjudication.model or (configured.model if configured else "")
    adjudicator_label = (
        f"{adjudicator_provider}:{adjudicator_model}" if adjudicator_provider else ""
    )
    adjudicator_record = {
        "provider_id": adjudicator_provider,
        "model": adjudicator_model,
        "routing": routing_summary,
        "called": adjudication.called,
        "ok": adjudication.ok if adjudication.called else None,
        "prompt_version": ADJUDICATION_PROMPT_VERSION,
        "feedback_source": feedback_source,
    }
    panel_block = reconciliation.build_panel_block(
        sheets=sheets,
        adjudicator=adjudicator_record,
        agreement=agreement,
        criteria=reconciliation.build_panel_criteria(aligned, resolutions),
        tie_break=tie_break,
        warnings=warnings,
    )

    final_sheet: dict[str, Any] = {
        "session_id": session_id,
        "transcript_file": to_repo_relative(transcript_path),
        "case_study_file": to_repo_relative(case_study_path),
        "rubric_file": "embedded_in_case_study_pdf",
        "rubric_source": f"{to_repo_relative(case_study_path)}#rubric-section",
        "criteria": final_criteria,
        "keep_start_stop": feedback["keep_start_stop"],
        "overall_summary": feedback["overall_summary"],
        "model": reconciliation.panel_model_label(sheets, adjudicator_label or None),
        "model_provider": reconciliation.MARKING_MODE_PANEL,
        "prompt_version": sheets[0].prompt_version or "",
        "marking_mode": reconciliation.MARKING_MODE_PANEL,
        "panel": panel_block,
        "path_checks": {
            "transcript_folder_matches_file_stem": transcript_path.parent.name == transcript_path.stem
        },
    }
    if warnings:
        final_sheet["warnings"] = warnings
    final_sheet["scoring_summary"] = compute_scoring_summary(final_criteria)

    record = {
        "schema": ADJUDICATION_RECORD_SCHEMA,
        "session_id": session_id,
        "prompt_version": ADJUDICATION_PROMPT_VERSION,
        "tie_break": str(tie_break),
        "markers": [sheet.describe() for sheet in sheets],
        "agreement": agreement,
        "disputes": [
            {**request, "index": request["index"] - 1}  # back to the sheet's 0-based index
            for request in requests
        ],
        "adjudication": {**adjudication.describe(), "raw_content": adjudication.raw_content},
        "feedback": {**feedback_outcome.describe(), "source": feedback_source, "raw_content": feedback_outcome.raw_content},
        "resolutions": [
            {
                "index": resolution.index,
                "value": resolution.value,
                "resolution": resolution.resolution,
                "timestamp": resolution.timestamp,
                "reason": resolution.reason,
                "sided_with": resolution.sided_with,
                "confidence": resolution.confidence,
            }
            for resolution in sorted(resolutions.values(), key=lambda item: item.index)
        ],
    }

    write_text_atomic(adjudication_path, json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    output_json = json.dumps(final_sheet, indent=2, ensure_ascii=False)
    write_text_atomic(output_path, output_json + "\n")
    checkpoint_path.unlink(missing_ok=True)
    print(f"Saved: {output_path}", file=sys.stderr)
    print(output_json)
    return 0


def main() -> int:
    args = parse_args()
    router = None if args.without_adjudicator else build_router_from_env()
    if router is not None and not router.resolved_targets():
        print(
            "[osce_panel_adjudicator] no adjudicator target is configured; disputes fall to the tie-break.",
            file=sys.stderr,
        )
        router = None
    return run_panel(args, router)


if __name__ == "__main__":
    run_main(main, script_name="osce_panel_adjudicator")
