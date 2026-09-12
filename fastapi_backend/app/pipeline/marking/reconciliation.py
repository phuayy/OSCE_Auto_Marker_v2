"""Reconciling several markers' sheets into one — the part that needs no model.

A content sheet is structured: one Yes/No per rubric criterion, in rubric
order, plus a timestamp and a reason. Two sheets for the same consultation
therefore agree or disagree *per criterion*, and that is decided here, in
code. Only the criteria the markers split on are ever put to the adjudicating
model; everything unanimous is settled without a call, and the agreement
statistics that describe how alike the markers were are computed once and
recorded on the final sheet.

This module is deliberately free of I/O and of the rest of the pipeline: the
adjudicator script (a subprocess) and the API (which assembles a degraded sheet
in-process when only one marker survives) both import it, so it must stay
importable from either side of that boundary with nothing but the LLM
vocabulary and the shared timestamp helpers.
"""
from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.scorer_utils import format_timestamp_seconds, normalize_timestamp
from app.llm.panel import TieBreak

YES = "Yes"
NO = "No"

# Schema stamped on the ``panel`` block of a final sheet. Bump when the block's
# shape changes; readers (the frontend, analytics) key on it.
PANEL_BLOCK_SCHEMA = "content-panel-v1"

# The ``marking_mode`` a final sheet carries. A sheet written before marking
# modes existed carries none, which readers treat as single.
MARKING_MODE_SINGLE = "single"
MARKING_MODE_PANEL = "panel"

# How a criterion on the final sheet was decided.
RESOLUTION_AGREED = "agreed"
RESOLUTION_ADJUDICATED = "adjudicated"
RESOLUTION_TIE_BREAK_PREFIX = "tie_break:"
# The one marker still standing decided everything — a degraded panel.
RESOLUTION_SOLE_MARKER = "sole_marker"

# The placeholder timestamp the validator writes when a marker cited none.
UNKNOWN_TIMESTAMP = "00:00:00"

# Seconds of transcript either side of a cited moment that the adjudicator is
# shown. Long enough to hold the exchange a criterion is about, short enough
# that two or three disputes stay well inside a single request.
DEFAULT_EVIDENCE_WINDOW_SECONDS = 45.0
MAX_EVIDENCE_WINDOW_CHARS = 4_000


def tie_break_resolution(policy: TieBreak | str) -> str:
    return f"{RESOLUTION_TIE_BREAK_PREFIX}{TieBreak(str(policy))}"


def is_tie_break_resolution(resolution: Any) -> bool:
    return str(resolution or "").startswith(RESOLUTION_TIE_BREAK_PREFIX)


class SheetAlignmentError(ValueError):
    """Two sheets do not describe the same rubric.

    Raised, never worked around: a sheet with a different criterion count or a
    different label at some index was marked against a different rubric (or a
    different extraction of it), and reconciling it item-by-item would compare
    unrelated criteria while looking like agreement.
    """


@dataclass(frozen=True)
class MarkerVote:
    """One marker's answer to one criterion."""

    value: str
    timestamp: str = UNKNOWN_TIMESTAMP
    reason: str = ""

    @property
    def cites_evidence(self) -> bool:
        return bool(self.timestamp) and self.timestamp != UNKNOWN_TIMESTAMP


@dataclass(frozen=True)
class MarkerSheet:
    """One marker's validated sheet, in the shape the assessor script writes.

    ``key`` is the marker's file name stem — ``marker_key(target)`` on the API
    side — and is what the final sheet uses to refer to it. ``provider_id`` and
    ``model`` are what the sheet itself recorded: the model that *actually*
    produced it, which is the provenance worth keeping.
    """

    key: str
    provider_id: str
    model: str
    criteria: tuple[MarkerVote, ...]
    keep_start_stop: Mapping[str, str] = field(default_factory=dict)
    overall_summary: str = ""
    scoring_summary: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    prompt_version: str = ""

    @classmethod
    def from_payload(cls, key: str, payload: Mapping[str, Any]) -> "MarkerSheet":
        criteria_raw = payload.get("criteria")
        votes: list[MarkerVote] = []
        for item in criteria_raw if isinstance(criteria_raw, list) else []:
            if not isinstance(item, Mapping):
                continue
            votes.append(
                MarkerVote(
                    value=str(item.get("value") or "").strip(),
                    timestamp=str(item.get("timestamp") or UNKNOWN_TIMESTAMP).strip() or UNKNOWN_TIMESTAMP,
                    reason=str(item.get("reason") or "").strip(),
                )
            )
        feedback_raw = payload.get("keep_start_stop")
        feedback = (
            {str(name): str(text or "").strip() for name, text in feedback_raw.items()}
            if isinstance(feedback_raw, Mapping)
            else {}
        )
        summary_raw = payload.get("scoring_summary")
        warnings_raw = payload.get("warnings")
        return cls(
            key=str(key),
            provider_id=str(payload.get("model_provider") or "").strip(),
            model=str(payload.get("model") or "").strip(),
            criteria=tuple(votes),
            keep_start_stop=feedback,
            overall_summary=str(payload.get("overall_summary") or "").strip(),
            scoring_summary=dict(summary_raw) if isinstance(summary_raw, Mapping) else {},
            warnings=tuple(str(item) for item in warnings_raw) if isinstance(warnings_raw, list) else (),
            prompt_version=str(payload.get("prompt_version") or "").strip(),
        )

    @property
    def target_label(self) -> str:
        return f"{self.provider_id}:{self.model}" if self.provider_id else self.model or self.key

    @property
    def pass_fail(self) -> str:
        return str(self.scoring_summary.get("pass_fail") or "").strip()

    def describe(self) -> dict[str, Any]:
        """The marker entry on the final sheet's ``panel.markers`` list."""
        return {
            "key": self.key,
            "provider_id": self.provider_id,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "summary": {
                "yes_count": self.scoring_summary.get("yes_count"),
                "critical_no": self.scoring_summary.get("critical_no"),
                "pass_fail": self.pass_fail,
            },
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class AlignedCriterion:
    """One rubric criterion with every marker's vote on it, in marker order."""

    index: int
    label: str
    is_critical: bool
    votes: tuple[MarkerVote, ...]

    @property
    def values(self) -> tuple[str, ...]:
        return tuple(vote.value for vote in self.votes)

    @property
    def unanimous(self) -> bool:
        return len(set(self.values)) <= 1

    def settled_vote(self) -> MarkerVote:
        """The vote a unanimous criterion carries onto the final sheet.

        Every marker said the same thing; the one that cited a real moment in
        the transcript is the more useful record, so it wins ties on evidence.
        Otherwise the first marker's vote stands, which keeps the choice
        deterministic.
        """
        for vote in self.votes:
            if vote.cites_evidence:
                return vote
        return self.votes[0]


def _fold_label(label: str) -> str:
    return " ".join(str(label or "").split()).casefold()


def align_sheets(
    sheets: Sequence[MarkerSheet],
    rubric_criteria: Sequence[Mapping[str, Any]],
) -> tuple[AlignedCriterion, ...]:
    """Line the sheets up against the rubric, criterion by criterion.

    Each sheet was validated against the same extracted rubric, so index
    alignment is the contract; the label check is the guard that catches a
    sheet which was not.
    """
    if not sheets:
        raise SheetAlignmentError("No marker sheets to align.")
    expected = len(rubric_criteria)
    if expected == 0:
        raise SheetAlignmentError("The rubric has no criteria to align against.")
    for sheet in sheets:
        if len(sheet.criteria) != expected:
            raise SheetAlignmentError(
                f"Marker {sheet.key} scored {len(sheet.criteria)} criteria; the rubric has {expected}."
            )
        for vote in sheet.criteria:
            if vote.value not in (YES, NO):
                raise SheetAlignmentError(
                    f"Marker {sheet.key} carries a value other than Yes/No ({vote.value!r}); "
                    "the sheet was not validated."
                )

    aligned: list[AlignedCriterion] = []
    for index, expected_item in enumerate(rubric_criteria):
        label = str(expected_item.get("label") or f"Criterion {index + 1}").strip()
        aligned.append(
            AlignedCriterion(
                index=index,
                label=label,
                is_critical=bool(expected_item.get("is_critical")),
                votes=tuple(sheet.criteria[index] for sheet in sheets),
            )
        )
    return tuple(aligned)


def check_sheet_labels(key: str, rubric_labels: Sequence[str], sheet_labels: Sequence[str]) -> None:
    """Refuse a sheet whose criterion labels differ from the rubric's.

    A written sheet carries the rubric's own labels (the validator put them
    there), so a label that differs means the sheet was marked against another
    rubric, or another extraction of this one. Kept separate from
    :func:`align_sheets` because :class:`MarkerSheet` drops the labels; the
    caller that still holds the raw payload runs this first.
    """
    if len(rubric_labels) != len(sheet_labels):
        raise SheetAlignmentError(
            f"Marker {key} scored {len(sheet_labels)} criteria; the rubric has {len(rubric_labels)}."
        )
    for index, (expected, actual) in enumerate(zip(rubric_labels, sheet_labels, strict=True), start=1):
        if _fold_label(expected) != _fold_label(actual):
            raise SheetAlignmentError(
                f"Marker {key} criterion {index} is {actual!r}; the rubric says {expected!r}."
            )


@dataclass(frozen=True)
class Reconciliation:
    aligned: tuple[AlignedCriterion, ...]

    @property
    def settled(self) -> tuple[AlignedCriterion, ...]:
        return tuple(item for item in self.aligned if item.unanimous)

    @property
    def disputes(self) -> tuple[AlignedCriterion, ...]:
        return tuple(item for item in self.aligned if not item.unanimous)


def reconcile(aligned: Sequence[AlignedCriterion]) -> Reconciliation:
    """Split the criteria into the unanimous and the disputed.

    Unanimity is the only thing that settles a criterion without the
    adjudicator. With three or more markers a majority is deliberately *not*
    enough: the literature on judge panels finds plain majority voting close to
    chance on the hard items, and the hard items are exactly the disputed ones.
    """
    return Reconciliation(aligned=tuple(aligned))


def cohen_kappa(first: Sequence[str], second: Sequence[str]) -> float | None:
    """Cohen's κ between two markers' Yes/No columns.

    ``None`` when it is undefined — no items, or both markers constant, where
    the expected agreement equals one and the ratio has no meaning. Reported
    beside the percent agreement, never instead of it: on twenty-odd items κ
    swings widely, and its value is in the aggregate across sessions.
    """
    if len(first) != len(second) or not first:
        return None
    total = len(first)
    observed = sum(1 for a, b in zip(first, second, strict=True) if a == b) / total
    yes_a = sum(1 for value in first if value == YES) / total
    yes_b = sum(1 for value in second if value == YES) / total
    expected = yes_a * yes_b + (1 - yes_a) * (1 - yes_b)
    if math.isclose(expected, 1.0):
        return None
    return round((observed - expected) / (1 - expected), 4)


def agreement_summary(
    reconciliation: Reconciliation,
    sheets: Sequence[MarkerSheet],
) -> dict[str, Any]:
    """The ``panel.agreement`` block: how alike the markers were, before anyone
    settled anything."""
    total = len(reconciliation.aligned)
    disputed = len(reconciliation.disputes)
    agreed = total - disputed
    kappa = (
        cohen_kappa(
            [vote.value for vote in sheets[0].criteria],
            [vote.value for vote in sheets[1].criteria],
        )
        if len(sheets) == 2
        else None
    )
    verdicts = {sheet.pass_fail for sheet in sheets if sheet.pass_fail}
    return {
        "total": total,
        "agreed": agreed,
        "disputed": disputed,
        "percent": round(agreed / total, 4) if total else None,
        "cohen_kappa": kappa,
        "pass_fail_agreed": len(verdicts) <= 1 if verdicts else None,
        "disputed_indexes": [item.index for item in reconciliation.disputes],
    }


def apply_tie_break(criterion: AlignedCriterion, policy: TieBreak | str) -> str:
    """The value a disputed criterion takes when no adjudicator can decide it."""
    policy = TieBreak(str(policy))
    if policy is TieBreak.LENIENT:
        return YES
    if policy is TieBreak.STRICT:
        return NO
    return criterion.votes[0].value


def shuffled_order(seed: str, count: int) -> tuple[int, ...]:
    """A permutation of ``range(count)`` that is the same for the same seed.

    The adjudicator is shown the markers' positions in this order and told it
    carries no meaning, so a model's tendency to favour whichever answer it
    reads first spreads evenly rather than always landing on marker one.
    Seeded by the session and the criterion so a re-run reproduces the same
    prompt — and the same checkpoint.
    """
    order = list(range(count))
    random.Random(seed).shuffle(order)
    return tuple(order)


def timestamp_to_seconds(value: Any) -> float | None:
    normalised = normalize_timestamp(value)
    if normalised is None:
        return None
    hours, minutes, seconds = (int(part) for part in normalised.split(":"))
    return float(hours * 3600 + minutes * 60 + seconds)


def transcript_window(
    transcript: Mapping[str, Any],
    timestamp: Any,
    *,
    seconds: float = DEFAULT_EVIDENCE_WINDOW_SECONDS,
    max_chars: int = MAX_EVIDENCE_WINDOW_CHARS,
) -> str:
    """The transcript segments around a cited moment, rendered as the markers
    saw them (``[SPEAKER] start - end: text``).

    Empty when the timestamp is missing or nothing was said in the window: the
    adjudicator is told so rather than shown an unrelated stretch of dialogue.
    """
    centre = timestamp_to_seconds(timestamp)
    if centre is None:
        return ""
    low, high = max(0.0, centre - seconds), centre + seconds
    lines: list[str] = []
    segments = transcript.get("segments")
    for segment in segments if isinstance(segments, list) else []:
        if not isinstance(segment, Mapping):
            continue
        try:
            start = float(segment.get("start"))
            end = float(segment.get("end", start))
        except (TypeError, ValueError):
            continue
        if end < low or start > high:
            continue
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        speaker = str(segment.get("speaker") or "SPEAKER_UNKNOWN").strip() or "SPEAKER_UNKNOWN"
        lines.append(
            f"[{speaker}] {format_timestamp_seconds(start, include_ms=False)} - "
            f"{format_timestamp_seconds(end, include_ms=False)}: {text}"
        )
    rendered = "\n".join(lines)
    if len(rendered) > max_chars:
        rendered = rendered[:max_chars] + "\n[window truncated]"
    return rendered


def closest_sheet_index(final_values: Sequence[str], sheets: Sequence[MarkerSheet]) -> int:
    """Which marker's verdicts most resemble the final ones — the sheet whose
    coaching feedback is the least wrong to reuse when it cannot be merged."""
    best_index, best_matches = 0, -1
    for position, sheet in enumerate(sheets):
        matches = sum(
            1 for vote, final in zip(sheet.criteria, final_values, strict=False) if vote.value == final
        )
        if matches > best_matches:
            best_index, best_matches = position, matches
    return best_index


@dataclass(frozen=True)
class Resolution:
    """How one disputed criterion was settled."""

    index: int
    value: str
    resolution: str
    timestamp: str = UNKNOWN_TIMESTAMP
    reason: str = ""
    # Position (in marker order) of the marker the adjudicator agreed with,
    # or ``None`` when it agreed with nobody or a tie-break decided.
    sided_with: int | None = None
    confidence: float | None = None


def tie_break_resolution_for(criterion: AlignedCriterion, policy: TieBreak | str) -> Resolution:
    value = apply_tie_break(criterion, policy)
    # Reuse the evidence of a marker that voted this way, if one did — the
    # final sheet then still points at a moment in the recording.
    supporting = [vote for vote in criterion.votes if vote.value == value]
    cited = next((vote for vote in supporting if vote.cites_evidence), supporting[0] if supporting else None)
    policy_name = TieBreak(str(policy))
    return Resolution(
        index=criterion.index,
        value=value,
        resolution=tie_break_resolution(policy_name),
        timestamp=cited.timestamp if cited else UNKNOWN_TIMESTAMP,
        reason=(
            f"Markers disagreed and no adjudication was available; settled by the "
            f"'{policy_name}' tie-break policy."
        ),
        sided_with=next(
            (position for position, vote in enumerate(criterion.votes) if vote is cited), None
        ),
    )


def build_final_criteria(
    aligned: Sequence[AlignedCriterion],
    resolutions: Mapping[int, Resolution],
) -> list[dict[str, Any]]:
    """The ``criteria`` list of the final sheet, in the assessor's own shape."""
    final: list[dict[str, Any]] = []
    for criterion in aligned:
        if criterion.unanimous:
            vote = criterion.settled_vote()
            final.append(
                {
                    "label": criterion.label,
                    "is_critical": criterion.is_critical,
                    "value": vote.value,
                    "timestamp": vote.timestamp,
                    "reason": vote.reason,
                }
            )
            continue
        resolution = resolutions.get(criterion.index)
        if resolution is None:
            raise ValueError(f"Disputed criterion {criterion.index + 1} has no resolution.")
        final.append(
            {
                "label": criterion.label,
                "is_critical": criterion.is_critical,
                "value": resolution.value,
                "timestamp": resolution.timestamp or UNKNOWN_TIMESTAMP,
                "reason": resolution.reason,
            }
        )
    return final


def build_panel_criteria(
    aligned: Sequence[AlignedCriterion],
    resolutions: Mapping[int, Resolution],
) -> list[dict[str, Any]]:
    """The ``panel.criteria`` list: every vote, and how each item was decided."""
    entries: list[dict[str, Any]] = []
    for criterion in aligned:
        entry: dict[str, Any] = {
            "index": criterion.index,
            "votes": list(criterion.values),
            "reasons": [vote.reason for vote in criterion.votes],
            "timestamps": [vote.timestamp for vote in criterion.votes],
        }
        if criterion.unanimous:
            entry["resolution"] = RESOLUTION_AGREED
        else:
            resolution = resolutions[criterion.index]
            entry["resolution"] = resolution.resolution
            entry["sided_with"] = resolution.sided_with
            entry["confidence"] = resolution.confidence
            entry["reason"] = resolution.reason
        entries.append(entry)
    return entries


def build_panel_block(
    *,
    sheets: Sequence[MarkerSheet],
    adjudicator: Mapping[str, Any],
    agreement: Mapping[str, Any] | None,
    criteria: Sequence[Mapping[str, Any]],
    tie_break: TieBreak | str,
    degraded: Mapping[str, Any] | None = None,
    warnings: Sequence[str] = (),
) -> dict[str, Any]:
    """The ``panel`` block of a final sheet, whichever process assembles it."""
    return {
        "schema": PANEL_BLOCK_SCHEMA,
        "markers": [sheet.describe() for sheet in sheets],
        "adjudicator": dict(adjudicator),
        "agreement": dict(agreement) if agreement is not None else None,
        "criteria": [dict(item) for item in criteria],
        "tie_break": str(TieBreak(str(tie_break))),
        "degraded": dict(degraded) if degraded else None,
        "warnings": list(warnings),
    }


def panel_model_label(sheets: Sequence[MarkerSheet], adjudicator_label: str | None) -> str:
    """The ``model`` field of a panel sheet — one line naming everyone who
    took part, for the places that show a single model name."""
    markers = " + ".join(sheet.target_label for sheet in sheets)
    if adjudicator_label:
        return f"panel({markers} -> {adjudicator_label})"
    return f"panel({markers})"


def degraded_panel_sheet(
    sheet_payload: Mapping[str, Any],
    sheet: MarkerSheet,
    *,
    reason: str,
    tie_break: TieBreak | str,
    warnings: Sequence[str] = (),
) -> dict[str, Any]:
    """A final sheet made from the one marker that survived.

    The marker's own sheet is kept verbatim — its marks, its evidence, its
    feedback — and the panel block records that this is a panel of one and
    why, so nothing downstream mistakes it for a reconciled result.
    """
    final = dict(sheet_payload)
    final["marking_mode"] = MARKING_MODE_PANEL
    final["model"] = panel_model_label([sheet], None)
    final["model_provider"] = MARKING_MODE_PANEL
    final["panel"] = build_panel_block(
        sheets=[sheet],
        adjudicator={"provider_id": "", "model": "", "called": False, "reason": "single marker"},
        agreement=None,
        criteria=[
            {
                "index": index,
                "votes": [vote.value],
                "reasons": [vote.reason],
                "timestamps": [vote.timestamp],
                "resolution": RESOLUTION_SOLE_MARKER,
            }
            for index, vote in enumerate(sheet.criteria)
        ],
        tie_break=tie_break,
        degraded={"reason": reason, "effective_mode": MARKING_MODE_SINGLE, "marker": sheet.key},
        warnings=warnings,
    )
    return final


__all__ = [
    "DEFAULT_EVIDENCE_WINDOW_SECONDS",
    "MARKING_MODE_PANEL",
    "MARKING_MODE_SINGLE",
    "NO",
    "PANEL_BLOCK_SCHEMA",
    "RESOLUTION_ADJUDICATED",
    "RESOLUTION_AGREED",
    "RESOLUTION_SOLE_MARKER",
    "RESOLUTION_TIE_BREAK_PREFIX",
    "UNKNOWN_TIMESTAMP",
    "YES",
    "AlignedCriterion",
    "MarkerSheet",
    "MarkerVote",
    "Reconciliation",
    "Resolution",
    "SheetAlignmentError",
    "agreement_summary",
    "align_sheets",
    "apply_tie_break",
    "build_final_criteria",
    "build_panel_block",
    "build_panel_criteria",
    "check_sheet_labels",
    "closest_sheet_index",
    "cohen_kappa",
    "degraded_panel_sheet",
    "is_tie_break_resolution",
    "panel_model_label",
    "reconcile",
    "shuffled_order",
    "tie_break_resolution",
    "tie_break_resolution_for",
    "timestamp_to_seconds",
    "transcript_window",
]
