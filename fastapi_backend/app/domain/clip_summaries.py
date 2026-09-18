"""Per-clip cohort summary rows for a long recording's Analytics view.

``ClipService.clip_summaries`` (the ``GET /sessions/{id}/clip-summaries``
handler behind ``LongVideoSummaryCharts``) used to build these rows inline in
one 86-line loop — nested ``.get(...) or {}`` chains and ternaries assembling
the content/communication dicts by hand, no schema in sight. That function
measured cyclomatic complexity 71 (grade F): nothing here does file or
database I/O, so none of that branching was ever about the fetch, only about
shaping data already in memory. It is a pure transform now, importable and
unit-testable without a session, a database, or a filesystem — the same
argument that moved the frontend's score derivations into
``src/lib/resultsModel.js``.

Callers pass already-loaded score JSON (or ``None`` when a child has not
been scored yet); the boundary that reads those files
(``ClipService._read_json_if_exists``) already guarantees a ``dict | None``
result, so the functions here trust that rather than re-checking it.
"""

from __future__ import annotations

from typing import Any


def clip_order(clips: list[dict[str, Any]], clip_id: str) -> int:
    """Index of ``clip_id`` in the parent's clip list, or -1 if not found."""
    for index, clip in enumerate(clips):
        if str(clip.get("id")) == str(clip_id):
            return index
    return -1


def resolve_clip_label(clip_source: dict[str, Any], clip: dict[str, Any] | None, child: dict[str, Any]) -> str:
    """The clip label a summary row shows: the run's own snapshot, the clip's
    current label, the child session's name, or a fallback naming the child."""
    explicit = str(clip_source.get("label") or "").strip()
    if explicit:
        return explicit
    if clip and clip.get("label"):
        return str(clip["label"])
    if child.get("name"):
        return str(child["name"])
    return f"Clip {str(child.get('id') or '')[:8]}"


def summarize_content_scores(content_scores: dict[str, Any] | None) -> dict[str, Any] | None:
    """The content tab's numbers, or None when the clip has no content sheet yet."""
    if not content_scores:
        return None
    summary = content_scores.get("scoring_summary") or {}
    criteria = content_scores.get("criteria")
    criteria = criteria if isinstance(criteria, list) else []
    total = len(criteria) if criteria else int(summary.get("total_criteria") or 0)
    yes_count = int(summary.get("yes_count") or 0)
    percent_yes = round((yes_count / total) * 1000) / 10 if total > 0 else 0
    return {
        "totalCriteria": total,
        "yesCount": yes_count,
        "noCount": int(summary.get("no_count") or 0),
        "criticalYes": int(summary.get("critical_yes") or 0),
        "criticalNo": int(summary.get("critical_no") or 0),
        "passFail": str(summary.get("pass_fail") or ""),
        "percentYes": percent_yes,
    }


def _communication_criterion_point(index: int, criterion: Any) -> dict[str, Any]:
    criterion = criterion if isinstance(criterion, dict) else {}
    return {
        "id": criterion.get("id", index + 1),
        "label": str(criterion.get("label") or f"Criterion {index + 1}"),
        "section": criterion.get("section"),
        "scoreLabel": str(criterion.get("score_label") or "None"),
        "points": float(criterion.get("points") or 0),
    }


def summarize_communication_scores(communication_scores: dict[str, Any] | None) -> dict[str, Any] | None:
    """The communication tab's numbers, or None when the clip has no sheet yet."""
    if not communication_scores:
        return None
    summary = communication_scores.get("scoring_summary") or {}
    criteria = communication_scores.get("criteria")
    criteria = criteria if isinstance(criteria, list) else []
    return {
        "totalCriteria": int(summary.get("total_criteria") or len(criteria)),
        "totalScore": float(summary.get("total_score") or 0),
        "maxScore": float(summary.get("max_score") or len(criteria) * 3),
        "passThreshold": float(summary.get("pass_threshold") or 0),
        "passFail": str(summary.get("pass_fail") or ""),
        "labelCounts": summary.get("label_counts") or {},
        "perCriterionPoints": [_communication_criterion_point(index, item) for index, item in enumerate(criteria)],
    }


def build_clip_summary_row(
    child: dict[str, Any],
    *,
    clips: list[dict[str, Any]],
    clips_by_id: dict[str, dict[str, Any]],
    content_scores: dict[str, Any] | None,
    communication_scores: dict[str, Any] | None,
) -> dict[str, Any]:
    """One cohort-table row for a clip's child session."""
    clip_source = child.get("clipSource") if isinstance(child.get("clipSource"), dict) else {}
    current_clip_id = str(clip_source.get("clipId") or "")
    clip = clips_by_id.get(current_clip_id)
    return {
        "sessionId": str(child.get("id") or ""),
        "sessionName": child.get("name") or None,
        "clipId": current_clip_id,
        "clipLabel": resolve_clip_label(clip_source, clip, child),
        "clipOrder": clip_order(clips, current_clip_id),
        "status": str(child.get("status") or "").lower(),
        "content": summarize_content_scores(content_scores),
        "communication": summarize_communication_scores(communication_scores),
    }


def aggregate_clip_summaries(
    *, parent_id: str, clips: list[dict[str, Any]], rows: list[dict[str, Any]], child_count: int
) -> dict[str, Any]:
    """The full `/clip-summaries` payload: rows in clip order, plus cohort counts."""
    ordered = sorted(rows, key=lambda item: (item["clipOrder"] if item["clipOrder"] >= 0 else 10**9, str(item["sessionId"])))
    assessed_count = len([item for item in ordered if item["content"] and item["communication"]])
    return {
        "parentSessionId": parent_id,
        "clipsCount": len(clips),
        "totalChildSessions": child_count,
        "assessedCount": assessed_count,
        "summaries": ordered,
    }
