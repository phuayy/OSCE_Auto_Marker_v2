from __future__ import annotations

import importlib
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _scorer():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("nvidia_osce_communication")


def _rubric(count: int = 2, **extra):
    return {
        "title": "R",
        "schema": "communication-rubric-v1",
        "criteria": [{"id": i + 1, "label": f"L{i}", "indicators": []} for i in range(count)],
        **extra,
    }


def _payload(labels: list[str]):
    return {
        "session_id": "s",
        "criteria": [
            {"id": i + 1, "score_label": label, "evidence": "e", "timestamp": "00:00:05",
             "indicators_observed": []}
            for i, label in enumerate(labels)
        ],
        "overall_summary": "ok",
    }


def test_default_rubric_uses_3_2_1_0_and_count_times_three() -> None:
    rubric = _rubric(4)
    sheet, _ = _scorer().validate_output(_payload(["All", "Most", "Some", "None"]), rubric)
    assert [c["points"] for c in sheet["criteria"]] == [3, 2, 1, 0]
    assert sheet["scoring_scale"] == {"All": 3, "Most": 2, "Some": 1, "None": 0}
    assert sheet["scoring_summary"]["max_score"] == 12
    assert sheet["scoring_summary"]["total_score"] == 6


def test_custom_scale_drives_points_total_and_pass_fail() -> None:
    scale = {"All": 4, "Most": 2, "Some": 1, "None": 0}
    rubric = _rubric(2, scoring_scale=scale, max_score=8, pass_threshold=5)
    sheet, _ = _scorer().validate_output(_payload(["All", "Some"]), rubric)
    assert [c["points"] for c in sheet["criteria"]] == [4, 1]
    assert sheet["scoring_scale"] == scale
    summary = sheet["scoring_summary"]
    assert summary["total_score"] == 5 and summary["max_score"] == 8
    assert summary["pass_fail"] == "Pass"
    sheet, _ = _scorer().validate_output(_payload(["Most", "Some"]), rubric)
    assert sheet["scoring_summary"]["total_score"] == 3
    assert sheet["scoring_summary"]["pass_fail"] == "Fail"


def test_invalid_scale_falls_back_to_default_with_warning(capsys) -> None:
    rubric = _rubric(2, scoring_scale={"All": 1, "Most": 5, "Some": 1, "None": 0})
    sheet, _ = _scorer().validate_output(_payload(["All", "Most"]), rubric)
    assert sheet["scoring_scale"] == _scorer().DEFAULT_SCORING_SCALE
    assert [c["points"] for c in sheet["criteria"]] == [3, 2]
    assert "scoring_scale" in capsys.readouterr().err


def test_missing_max_score_falls_back_to_count_times_all() -> None:
    scale = {"All": 5, "Most": 3, "Some": 1, "None": 0}
    sheet, _ = _scorer().validate_output(_payload(["All", "All", "All"]), _rubric(3, scoring_scale=scale))
    assert sheet["scoring_summary"]["max_score"] == 15
    assert sheet["scoring_summary"]["pass_threshold"] == 8
