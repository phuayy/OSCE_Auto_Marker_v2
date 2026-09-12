"""Reconciling marker sheets without a model.

The rules a panel's final sheet rests on: unanimity settles a criterion and
nothing less does, agreement is described honestly (κ undefined rather than
faked), the tie-break is applied per policy with the evidence of a marker who
voted that way, and the transcript window shows the adjudicator the moment a
marker cited rather than the whole recording.
"""
from __future__ import annotations

import pytest

from app.llm.panel import TieBreak
from app.pipeline.marking import reconciliation as r

RUBRIC = [
    {"label": "Introduces self", "is_critical": True},
    {"label": "Asks about allergies", "is_critical": False},
    {"label": "Explains the plan", "is_critical": False},
]


def sheet(key: str, values: list[str], *, provider: str = "p", model: str = "m", timestamps=None, pass_fail="Pass"):
    timestamps = timestamps or ["00:00:10"] * len(values)
    return r.MarkerSheet.from_payload(
        key,
        {
            "model_provider": provider,
            "model": model,
            "criteria": [
                {"label": item["label"], "is_critical": item["is_critical"], "value": value, "timestamp": ts, "reason": f"{key} says {value}"}
                for item, value, ts in zip(RUBRIC, values, timestamps, strict=True)
            ],
            "keep_start_stop": {"keep": f"{key} keep", "start": f"{key} start", "stop": f"{key} stop"},
            "overall_summary": f"{key} summary",
            "scoring_summary": {"yes_count": values.count("Yes"), "pass_fail": pass_fail},
            "prompt_version": "content-marking-v1",
        },
    )


def test_unanimous_criteria_settle_and_the_rest_are_disputes() -> None:
    a = sheet("a", ["Yes", "Yes", "No"])
    b = sheet("b", ["Yes", "No", "No"])

    aligned = r.align_sheets([a, b], RUBRIC)
    reconciled = r.reconcile(aligned)

    assert [item.index for item in reconciled.settled] == [0, 2]
    assert [item.index for item in reconciled.disputes] == [1]
    assert reconciled.disputes[0].values == ("Yes", "No")
    assert reconciled.disputes[0].label == "Asks about allergies"


def test_three_markers_need_unanimity_not_a_majority() -> None:
    aligned = r.align_sheets(
        [sheet("a", ["Yes", "Yes", "Yes"]), sheet("b", ["Yes", "Yes", "No"]), sheet("c", ["Yes", "No", "No"])],
        RUBRIC,
    )
    reconciled = r.reconcile(aligned)
    assert [item.index for item in reconciled.settled] == [0]
    assert [item.index for item in reconciled.disputes] == [1, 2]


def test_a_sheet_from_a_different_rubric_is_refused() -> None:
    short = r.MarkerSheet(key="short", provider_id="p", model="m", criteria=(r.MarkerVote("Yes"), r.MarkerVote("No")))
    with pytest.raises(r.SheetAlignmentError, match="scored 2 criteria; the rubric has 3"):
        r.align_sheets([sheet("a", ["Yes", "Yes", "Yes"]), short], RUBRIC)

    with pytest.raises(r.SheetAlignmentError, match="criterion 2 is 'Asks about smoking'"):
        r.check_sheet_labels("x", [item["label"] for item in RUBRIC], ["Introduces self", "Asks about smoking", "Explains the plan"])


def test_an_unvalidated_sheet_is_refused() -> None:
    bad = r.MarkerSheet(key="bad", provider_id="p", model="m", criteria=(r.MarkerVote("Yes"), r.MarkerVote("maybe"), r.MarkerVote("No")))
    with pytest.raises(r.SheetAlignmentError, match="other than Yes/No"):
        r.align_sheets([sheet("a", ["Yes", "Yes", "Yes"]), bad], RUBRIC)


def test_settled_vote_prefers_the_marker_that_cited_a_moment() -> None:
    a = sheet("a", ["Yes", "Yes", "Yes"], timestamps=["00:00:00", "00:00:00", "00:00:00"])
    b = sheet("b", ["Yes", "Yes", "Yes"], timestamps=["00:01:00", "00:00:00", "00:02:00"])
    aligned = r.align_sheets([a, b], RUBRIC)

    assert aligned[0].settled_vote().timestamp == "00:01:00"
    assert aligned[0].settled_vote().reason == "b says Yes"
    # Nobody cited anything: the first marker's vote stands, deterministically.
    assert aligned[1].settled_vote().reason == "a says Yes"


def test_cohen_kappa_and_its_undefined_cases() -> None:
    assert r.cohen_kappa(["Yes", "Yes", "No", "No"], ["Yes", "Yes", "No", "No"]) == 1.0
    assert r.cohen_kappa(["Yes", "No"], ["No", "Yes"]) == -1.0
    # Both markers constant: expected agreement is 1, κ has no meaning.
    assert r.cohen_kappa(["Yes", "Yes"], ["Yes", "Yes"]) is None
    assert r.cohen_kappa([], []) is None
    assert r.cohen_kappa(["Yes"], ["Yes", "No"]) is None
    assert r.cohen_kappa(["Yes", "Yes", "No", "No"], ["Yes", "No", "No", "No"]) == 0.5


def test_agreement_summary_reports_percent_kappa_and_pass_fail() -> None:
    a = sheet("a", ["Yes", "Yes", "No"], pass_fail="Pass")
    b = sheet("b", ["Yes", "No", "No"], pass_fail="Fail")
    reconciled = r.reconcile(r.align_sheets([a, b], RUBRIC))

    summary = r.agreement_summary(reconciled, [a, b])

    assert summary["total"] == 3
    assert summary["agreed"] == 2
    assert summary["disputed"] == 1
    assert summary["percent"] == pytest.approx(2 / 3, abs=1e-4)
    assert summary["cohen_kappa"] is not None
    assert summary["pass_fail_agreed"] is False
    assert summary["disputed_indexes"] == [1]

    # κ is a two-rater statistic; with three sheets it is not reported.
    three = r.agreement_summary(reconciled, [a, b, sheet("c", ["Yes", "Yes", "No"])])
    assert three["cohen_kappa"] is None


@pytest.mark.parametrize(
    ("policy", "expected"),
    [(TieBreak.LENIENT, "Yes"), (TieBreak.STRICT, "No"), (TieBreak.FIRST_MARKER, "No")],
)
def test_tie_break_policies(policy: TieBreak, expected: str) -> None:
    aligned = r.align_sheets([sheet("a", ["No", "Yes", "Yes"]), sheet("b", ["Yes", "Yes", "Yes"])], RUBRIC)
    dispute = aligned[0]

    assert r.apply_tie_break(dispute, policy) == expected
    resolution = r.tie_break_resolution_for(dispute, policy)
    assert resolution.value == expected
    assert resolution.resolution == f"tie_break:{policy}"
    assert r.is_tie_break_resolution(resolution.resolution)
    # The evidence comes from a marker who voted that way.
    voter = 1 if expected == "Yes" else 0
    assert resolution.sided_with == voter
    assert resolution.timestamp == "00:00:10"


def test_shuffled_order_is_a_reproducible_permutation() -> None:
    first = r.shuffled_order("session:3", 2)
    assert sorted(first) == [0, 1]
    assert r.shuffled_order("session:3", 2) == first
    orders = {r.shuffled_order(f"session:{index}", 2) for index in range(40)}
    assert orders == {(0, 1), (1, 0)}, "over many criteria both orders must occur"


TRANSCRIPT = {
    "segments": [
        {"id": 1, "speaker": "SPEAKER_00", "start": 0.0, "end": 4.0, "text": "Hello, I'm the doctor."},
        {"id": 2, "speaker": "SPEAKER_01", "start": 60.0, "end": 63.0, "text": "I'm allergic to penicillin."},
        {"id": 3, "speaker": "SPEAKER_00", "start": 64.0, "end": 66.0, "text": "Noted, thank you."},
        {"id": 4, "speaker": "SPEAKER_00", "start": 400.0, "end": 402.0, "text": "Goodbye."},
    ]
}


def test_transcript_window_shows_only_the_cited_neighbourhood() -> None:
    window = r.transcript_window(TRANSCRIPT, "00:01:02")
    assert "penicillin" in window
    assert "Noted" in window
    assert "Goodbye" not in window
    assert "Hello" not in window
    assert window.splitlines()[0] == "[SPEAKER_01] 00:01:00 - 00:01:03: I'm allergic to penicillin."


def test_transcript_window_edge_cases() -> None:
    assert r.transcript_window(TRANSCRIPT, "00:00:00") == "[SPEAKER_00] 00:00:00 - 00:00:04: Hello, I'm the doctor."
    assert r.transcript_window(TRANSCRIPT, "garbage") == ""
    assert r.transcript_window({"segments": "nope"}, "00:00:01") == ""
    long_window = r.transcript_window(TRANSCRIPT, "00:01:02", max_chars=30)
    assert long_window.endswith("[window truncated]")


def test_closest_sheet_is_the_one_that_matches_the_final_verdicts_most() -> None:
    a = sheet("a", ["Yes", "Yes", "No"])
    b = sheet("b", ["No", "No", "No"])
    assert r.closest_sheet_index(["Yes", "Yes", "Yes"], [a, b]) == 0
    assert r.closest_sheet_index(["No", "No", "Yes"], [a, b]) == 1


def test_final_and_panel_criteria_are_built_from_votes_and_resolutions() -> None:
    a = sheet("a", ["Yes", "Yes", "No"], timestamps=["00:00:05", "00:00:00", "00:00:20"])
    b = sheet("b", ["Yes", "No", "No"], timestamps=["00:00:00", "00:00:11", "00:00:21"])
    aligned = r.align_sheets([a, b], RUBRIC)
    resolutions = {
        1: r.Resolution(index=1, value="No", resolution=r.RESOLUTION_ADJUDICATED, timestamp="00:00:11", reason="adjudged", sided_with=1, confidence=0.7)
    }

    final = r.build_final_criteria(aligned, resolutions)
    assert [item["value"] for item in final] == ["Yes", "No", "No"]
    assert final[0]["timestamp"] == "00:00:05"  # the citing marker's evidence
    assert final[1]["reason"] == "adjudged"
    assert final[2]["is_critical"] is False

    panel = r.build_panel_criteria(aligned, resolutions)
    assert panel[0]["resolution"] == r.RESOLUTION_AGREED
    assert panel[0]["votes"] == ["Yes", "Yes"]
    assert panel[1]["resolution"] == r.RESOLUTION_ADJUDICATED
    assert panel[1]["sided_with"] == 1
    assert panel[1]["reasons"] == ["a says Yes", "b says No"]

    with pytest.raises(ValueError, match="has no resolution"):
        r.build_final_criteria(aligned, {})


def test_degraded_sheet_keeps_the_survivor_verbatim_and_says_so() -> None:
    payload = {
        "criteria": [{"label": "x", "is_critical": True, "value": "Yes", "timestamp": "00:00:01", "reason": "r"}],
        "keep_start_stop": {"keep": "k", "start": "s", "stop": "t"},
        "overall_summary": "o",
        "model": "gemini-2.5-pro",
        "model_provider": "gemini",
        "scoring_summary": {"pass_fail": "Pass", "yes_count": 1},
    }
    survivor = r.MarkerSheet.from_payload("gemini__gemini-2-5-pro", payload)

    final = r.degraded_panel_sheet(payload, survivor, reason="nvidia died", tie_break=TieBreak.LENIENT, warnings=["Marker nvidia failed"])

    assert final["criteria"] == payload["criteria"]
    assert final["keep_start_stop"] == payload["keep_start_stop"]
    assert final["marking_mode"] == r.MARKING_MODE_PANEL
    assert final["model"] == "panel(gemini:gemini-2.5-pro)"
    assert final["model_provider"] == "panel"
    block = final["panel"]
    assert block["schema"] == r.PANEL_BLOCK_SCHEMA
    assert block["degraded"] == {"reason": "nvidia died", "effective_mode": "single", "marker": "gemini__gemini-2-5-pro"}
    assert block["markers"][0]["key"] == "gemini__gemini-2-5-pro"
    assert block["criteria"][0]["resolution"] == r.RESOLUTION_SOLE_MARKER
    assert block["agreement"] is None
    assert block["adjudicator"]["called"] is False
    assert block["warnings"] == ["Marker nvidia failed"]
