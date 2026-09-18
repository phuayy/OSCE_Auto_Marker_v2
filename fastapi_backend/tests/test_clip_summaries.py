"""Cohort-summary rows for a long recording's Analytics view.

Pure-transform tests for app.domain.clip_summaries — the module
ClipService.clip_summaries delegates to for everything that isn't I/O.
Pins the branch-heavy rules the old 86-line inline version buried: a clip
with no sheet yet reports None, not a zeroed-out dict; content and
communication summaries fall back independently; row order follows the
clip list, not scoring order; a child pointing at a clip the parent no
longer lists still gets a row.
"""

from __future__ import annotations

from app.domain.clip_summaries import (
    aggregate_clip_summaries,
    build_clip_summary_row,
    clip_order,
    resolve_clip_label,
    summarize_communication_scores,
    summarize_content_scores,
)

CLIPS = [
    {"id": "clip-a", "label": "Station A"},
    {"id": "clip-b", "label": "Station B"},
]


def test_clip_order_finds_index_and_reports_missing_as_negative_one() -> None:
    assert clip_order(CLIPS, "clip-b") == 1
    assert clip_order(CLIPS, "clip-nonexistent") == -1


def test_resolve_clip_label_prefers_the_runs_own_snapshot() -> None:
    clip_source = {"label": "  Snapshot label  "}
    clip = {"label": "Current clip label"}
    child = {"id": "child-1", "name": "Child name"}
    assert resolve_clip_label(clip_source, clip, child) == "Snapshot label"


def test_resolve_clip_label_falls_back_through_clip_then_child_name_then_id() -> None:
    child = {"id": "child-12345678", "name": "Child name"}
    assert resolve_clip_label({}, {"label": "Current clip label"}, child) == "Current clip label"
    assert resolve_clip_label({}, None, child) == "Child name"
    assert resolve_clip_label({}, None, {"id": "child-12345678"}) == "Clip child-12"


def test_summarize_content_scores_is_none_when_clip_not_scored_yet() -> None:
    assert summarize_content_scores(None) is None
    assert summarize_content_scores({}) is None


def test_summarize_content_scores_computes_percent_from_criteria_count() -> None:
    scores = {
        "criteria": [{}, {}, {}, {}],
        "scoring_summary": {"yes_count": 3, "no_count": 1, "critical_yes": 1, "critical_no": 0, "pass_fail": "Pass"},
    }
    result = summarize_content_scores(scores)
    assert result == {
        "totalCriteria": 4,
        "yesCount": 3,
        "noCount": 1,
        "criticalYes": 1,
        "criticalNo": 0,
        "passFail": "Pass",
        "percentYes": 75.0,
    }


def test_summarize_content_scores_falls_back_to_summary_total_when_criteria_list_absent() -> None:
    scores = {"scoring_summary": {"total_criteria": 5, "yes_count": 5, "pass_fail": "Pass"}}
    result = summarize_content_scores(scores)
    assert result["totalCriteria"] == 5
    assert result["percentYes"] == 100.0


def test_summarize_content_scores_zero_criteria_does_not_divide_by_zero() -> None:
    result = summarize_content_scores({"criteria": [], "scoring_summary": {}})
    assert result["totalCriteria"] == 0
    assert result["percentYes"] == 0


def test_summarize_communication_scores_is_none_when_clip_not_scored_yet() -> None:
    assert summarize_communication_scores(None) is None


def test_summarize_communication_scores_builds_per_criterion_points() -> None:
    scores = {
        "criteria": [
            {"id": "c1", "label": "Greets patient", "section": "Opening", "score_label": "Fully", "points": 3},
            {"label": "Explains risk"},
        ],
        "scoring_summary": {
            "total_score": 4,
            "max_score": 6,
            "pass_threshold": 3,
            "pass_fail": "Pass",
            "label_counts": {"Fully": 1, "None": 1},
        },
    }
    result = summarize_communication_scores(scores)
    assert result["totalCriteria"] == 2
    assert result["perCriterionPoints"][0] == {
        "id": "c1",
        "label": "Greets patient",
        "section": "Opening",
        "scoreLabel": "Fully",
        "points": 3.0,
    }
    # Second criterion has no explicit id/score, so it falls back by position.
    assert result["perCriterionPoints"][1]["id"] == 2
    assert result["perCriterionPoints"][1]["scoreLabel"] == "None"
    assert result["perCriterionPoints"][1]["points"] == 0.0


def test_build_clip_summary_row_carries_both_none_when_neither_scored() -> None:
    child = {"id": "child-1", "name": "Run 1", "status": "COMPLETED", "clipSource": {"clipId": "clip-a"}}
    row = build_clip_summary_row(
        child, clips=CLIPS, clips_by_id={c["id"]: c for c in CLIPS}, content_scores=None, communication_scores=None
    )
    assert row["sessionId"] == "child-1"
    assert row["clipId"] == "clip-a"
    assert row["clipOrder"] == 0
    assert row["status"] == "completed"
    assert row["content"] is None
    assert row["communication"] is None


def test_build_clip_summary_row_clip_no_longer_in_parents_list_still_returns_a_row() -> None:
    child = {"id": "child-2", "clipSource": {"clipId": "clip-deleted"}}
    row = build_clip_summary_row(
        child, clips=CLIPS, clips_by_id={c["id"]: c for c in CLIPS}, content_scores=None, communication_scores=None
    )
    assert row["clipOrder"] == -1


def test_aggregate_clip_summaries_orders_by_clip_position_not_scoring_order() -> None:
    row_b = build_clip_summary_row(
        {"id": "child-b", "clipSource": {"clipId": "clip-b"}},
        clips=CLIPS,
        clips_by_id={c["id"]: c for c in CLIPS},
        content_scores=None,
        communication_scores=None,
    )
    row_a = build_clip_summary_row(
        {"id": "child-a", "clipSource": {"clipId": "clip-a"}},
        clips=CLIPS,
        clips_by_id={c["id"]: c for c in CLIPS},
        content_scores=None,
        communication_scores=None,
    )
    result = aggregate_clip_summaries(parent_id="parent-1", clips=CLIPS, rows=[row_b, row_a], child_count=2)
    assert [row["sessionId"] for row in result["summaries"]] == ["child-a", "child-b"]
    assert result["clipsCount"] == 2
    assert result["totalChildSessions"] == 2
    assert result["assessedCount"] == 0


def test_aggregate_clip_summaries_counts_only_rows_scored_on_both_branches() -> None:
    scored_row = {"sessionId": "s1", "clipOrder": 0, "content": {"x": 1}, "communication": {"y": 1}}
    content_only_row = {"sessionId": "s2", "clipOrder": 1, "content": {"x": 1}, "communication": None}
    result = aggregate_clip_summaries(parent_id="p", clips=CLIPS, rows=[scored_row, content_only_row], child_count=2)
    assert result["assessedCount"] == 1


def test_unplaced_clips_sort_after_placed_ones() -> None:
    unplaced = {"sessionId": "s-unplaced", "clipOrder": -1, "content": None, "communication": None}
    placed = {"sessionId": "s-placed", "clipOrder": 0, "content": None, "communication": None}
    result = aggregate_clip_summaries(parent_id="p", clips=CLIPS, rows=[unplaced, placed], child_count=2)
    assert [row["sessionId"] for row in result["summaries"]] == ["s-placed", "s-unplaced"]
