"""Is this content sheet usable, and is it the sheet this run would produce?

Two questions, asked of files on disk before any model is called. The first —
*is the sheet well-formed?* — has been asked of ``scores/<id>.json`` since the
pipeline learnt to resume from cached artefacts, and applies unchanged to a
panel marker's own sheet. The second is new with marking modes: a well-formed
sheet can still be the wrong one — a single-model sheet when a panel was
selected, a marker sheet produced by a model the operator has since swapped
out, a panel sheet that degraded to one marker and deserves another try.
Answering both here keeps the pipeline's cache logic generic; it only needs a
predicate.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.llm.panel import MarkingMode
from app.pipeline.marking.base import MarkerAssignment, MarkingPlan
from app.pipeline.marking.fingerprint import sheet_marked_from
from app.pipeline.marking.reconciliation import (
    MARKING_MODE_PANEL,
    MARKING_MODE_SINGLE,
    is_tie_break_resolution,
)


def sheet_needs_refresh(payload: Any) -> bool:
    """True when a content sheet is missing, malformed or from an older schema.

    The historical ``ScoringPipeline.should_refresh_score_payload`` check,
    unchanged: it recognises the shape the assessor writes today and treats
    anything else as a reason to mark again.

    Deliberately *not* the place for the input fingerprint, even though it
    would close the same hole for the single-mode sheet. This predicate also
    runs over the final sheet (through ``final_sheet_needs_refresh``), and a
    panel's final sheet is written by ``osce_panel_adjudicator.py``, which
    carries no fingerprint — the check here would refresh every panel session
    forever. It belongs to ``marker_sheet_needs_refresh``, which knows it is
    looking at a sheet this API wrote.
    """
    if not isinstance(payload, dict):
        return True
    criteria = payload.get("criteria")
    if not isinstance(criteria, list) or len(criteria) < 2:
        return True
    if not all(isinstance(item, dict) and "is_critical" in item for item in criteria):
        return True
    summary = payload.get("scoring_summary")
    if not isinstance(summary, dict) or not summary.get("pass_fail"):
        return True
    critical_count = len([item for item in criteria if item.get("is_critical") is True])
    if int(summary.get("total_criteria") or -1) != len(criteria):
        return True
    if int(summary.get("critical_total") or -1) != critical_count:
        return True
    if str(payload.get("rubric_file") or "") != "embedded_in_case_study_pdf":
        return True
    if not payload.get("rubric_source"):
        return True
    if "transcript_quality_notes" in payload:
        return True
    return not all(
        isinstance(item.get("timestamp"), str) and item.get("timestamp")
        for item in criteria
        if isinstance(item, dict)
    )


def sheet_marking_mode(payload: Mapping[str, Any]) -> str:
    """The mode a sheet was produced under; sheets that predate modes are single."""
    return str(payload.get("marking_mode") or MARKING_MODE_SINGLE)


def sheet_matches_marker(payload: Any, assignment: MarkerAssignment) -> bool:
    """Was this marker sheet produced by the model the assignment names?

    The sheet records the provider and model that actually answered. The file
    is already named by the marker key, so a mismatch here means a stale file
    from before a swap — or a sheet a fallback wrote, which a marker must not
    have. An assignment with no explicit model accepts any model from its
    provider (the provider's default resolves at run time).
    """
    if not isinstance(payload, Mapping):
        return False
    if str(payload.get("model_provider") or "") != assignment.target.provider_id:
        return False
    wanted = assignment.target.model.strip()
    return not wanted or str(payload.get("model") or "").strip() == wanted


def marker_sheet_needs_refresh(
    payload: Any,
    assignment: MarkerAssignment,
    inputs: Mapping[str, Any] | None,
) -> bool:
    """Well-formed, written by the model this marker names, **and** marked from
    the files this run is handing it.

    The third clause has no default, so no call site can forget it. A marker
    sheet lives in ``scores/panel/<id>/``, which a re-run used not to remove:
    without this, a re-transcribed session was "marked" by adopting the sheets
    the markers wrote against the previous recording, and the adjudicator then
    reconciled marks describing two different conversations. Silently.
    """
    if sheet_needs_refresh(payload) or not sheet_matches_marker(payload, assignment):
        return True
    return not sheet_marked_from(payload, inputs)


def _panel_marker_keys(payload: Mapping[str, Any]) -> set[str]:
    panel = payload.get("panel")
    markers = panel.get("markers") if isinstance(panel, Mapping) else None
    return {
        str(item.get("key") or "")
        for item in (markers if isinstance(markers, list) else [])
        if isinstance(item, Mapping)
    }


def _panel_adjudicator_matches(payload: Mapping[str, Any], plan: MarkingPlan) -> bool:
    panel = payload.get("panel")
    recorded = panel.get("adjudicator") if isinstance(panel, Mapping) else None
    recorded = recorded if isinstance(recorded, Mapping) else {}
    if plan.adjudicator is None:
        # No adjudicator can run here; a sheet whose disputes an adjudicator
        # settled is still the better sheet, so nothing to refresh for.
        return True
    if str(recorded.get("provider_id") or "") != plan.adjudicator.target.provider_id:
        return False
    wanted = plan.adjudicator.target.model.strip()
    return not wanted or str(recorded.get("model") or "").strip() == wanted


def _panel_used_tie_break(payload: Mapping[str, Any]) -> bool:
    panel = payload.get("panel")
    criteria = panel.get("criteria") if isinstance(panel, Mapping) else None
    return any(
        isinstance(item, Mapping) and is_tie_break_resolution(item.get("resolution"))
        for item in (criteria if isinstance(criteria, list) else [])
    )


def final_sheet_needs_refresh(payload: Any, plan: MarkingPlan) -> bool:
    """Would this run produce a different final sheet than the one on disk?

    Beyond well-formedness, the sheet has to have been produced under the mode
    that will run, by the markers and adjudicator the plan names. A panel
    sheet that degraded to one marker is refreshed too — the failed marker
    gets its retry, and the surviving marker's own sheet is reused rather than
    re-marked. A sheet whose disputes fell to the tie-break is refreshed only
    when the policy has since changed.
    """
    if sheet_needs_refresh(payload):
        return True
    mode = sheet_marking_mode(payload)
    if plan.mode is MarkingMode.SINGLE:
        return mode != MARKING_MODE_SINGLE
    if mode != MARKING_MODE_PANEL:
        return True
    panel = payload.get("panel")
    if not isinstance(panel, Mapping):
        return True
    if panel.get("degraded"):
        return True
    if _panel_marker_keys(payload) != {assignment.key for assignment in plan.markers}:
        return True
    if not _panel_adjudicator_matches(payload, plan):
        return True
    return _panel_used_tie_break(payload) and str(panel.get("tie_break") or "") != str(plan.tie_break)


__all__ = [
    "final_sheet_needs_refresh",
    "marker_sheet_needs_refresh",
    "sheet_marking_mode",
    "sheet_matches_marker",
    "sheet_needs_refresh",
]
