"""The panel adjudicator script, driven end to end with a scripted model.

``run_panel`` is the script minus argument parsing and router construction, so
it can be handed a router whose provider replays canned replies. The cases are
the ones the final sheet's honesty depends on: unanimous criteria never reach
the model, the model decides only the disputes, its letter-coded verdict is
mapped back to the right marker, a bad reply is repaired, a dead model falls to
the tie-break per criterion, and the feedback merge falls back to the closest
marker's notes.
"""
from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from typing import Any

import pytest

from app.llm.base import LLMTransportError
from app.llm.router import LLMRouter
from app.llm.routing import LLMTarget, RetryPolicy, RoutingConfig
from tests.test_llm_router import ScriptedProvider

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

CASE_STUDY_TEXT = (
    "[Page 1]\nA 24-year-old presents with a sore throat and a rash. The student must take a "
    "history and explain the plan. This context is long enough to count as extractable text.\n\n"
    "[Page 4]\nAnalytical Checklist\nGATHERING INFORMATION/ INTRODUCTION YES NO\n"
    "1. Introduces self and confirms patient identity. [Critical criteria]\n"
    "2. Asks about drug allergies.\n"
    "3. Explains the management plan.\n"
    "References: PHR1012 notes."
)

TRANSCRIPT = {
    "schema": "whisperx-segments-v1",
    "segments": [
        {"id": 1, "speaker": "SPEAKER_00", "start": 0.0, "end": 4.0, "text": "Hello, I'm the pharmacist, can I confirm your name?"},
        {"id": 2, "speaker": "SPEAKER_01", "start": 60.0, "end": 63.0, "text": "I think I'm allergic to penicillin."},
        {"id": 3, "speaker": "SPEAKER_00", "start": 64.0, "end": 66.0, "text": "Noted, thank you."},
        {"id": 4, "speaker": "SPEAKER_00", "start": 120.0, "end": 125.0, "text": "So the plan is rest and fluids."},
    ],
}

LABELS = [
    "Introduces self and confirms patient identity",
    "Asks about drug allergies",
    "Explains the management plan",
]


@pytest.fixture(scope="module")
def adjudicator() -> dict[str, Any]:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    for name in ("content_marking", "scorer_checkpoint", "osce_panel_adjudicator"):
        sys.modules.pop(name, None)
    return runpy.run_path(str(SCRIPTS / "osce_panel_adjudicator.py"), run_name="adjudicator_under_test")


def marker_sheet(values: list[str], *, provider: str, model: str, timestamps: list[str] | None = None) -> dict[str, Any]:
    timestamps = timestamps or ["00:00:02", "00:01:01", "00:02:01"]
    return {
        "session_id": "s1",
        "rubric_file": "embedded_in_case_study_pdf",
        "rubric_source": "case.txt#rubric-section",
        "criteria": [
            {"label": label, "is_critical": index == 0, "value": value, "timestamp": ts, "reason": f"{model} reason {index}"}
            for index, (label, value, ts) in enumerate(zip(LABELS, values, timestamps, strict=True))
        ],
        "keep_start_stop": {"keep": f"{model} keep", "start": f"{model} start", "stop": f"{model} stop"},
        "overall_summary": f"{model} summary",
        "model": model,
        "model_provider": provider,
        "prompt_version": "content-marking-v1",
        "scoring_summary": {"total_criteria": 3, "yes_count": values.count("Yes"), "critical_total": 1, "pass_fail": "Pass" if values[0] == "Yes" else "Fail"},
    }


def write_inputs(tmp_path: Path, sheets: dict[str, dict[str, Any]]) -> dict[str, Path]:
    transcript = tmp_path / "transcripts" / "s1.json"
    transcript.parent.mkdir()
    transcript.write_text(json.dumps(TRANSCRIPT), encoding="utf-8")
    case = tmp_path / "case.txt"
    case.write_text(CASE_STUDY_TEXT, encoding="utf-8")
    panel_dir = tmp_path / "scores" / "panel" / "s1"
    panel_dir.mkdir(parents=True)
    paths = {"transcript": transcript, "case": case, "final": tmp_path / "scores" / "s1.json", "record": panel_dir / "adjudication.json"}
    for key, sheet in sheets.items():
        path = panel_dir / f"{key}.json"
        path.write_text(json.dumps(sheet), encoding="utf-8")
        paths[key] = path
    return paths


def args_for(adjudicator: dict[str, Any], paths: dict[str, Path], *marker_keys: str, **extra: Any):
    argv = [
        "--session-id", "s1",
        "--transcript", str(paths["transcript"]),
        "--case-study", str(paths["case"]),
        "--output", str(paths["final"]),
        "--adjudication-output", str(paths["record"]),
    ]
    for key in marker_keys:
        argv.extend(["--marker", str(paths[key])])
    for flag, value in extra.items():
        if value is True:
            argv.append(f"--{flag.replace('_', '-')}")
        elif isinstance(value, list):
            for item in value:
                argv.extend([f"--{flag.replace('_', '-')}", str(item)])
        else:
            argv.extend([f"--{flag.replace('_', '-')}", str(value)])
    return adjudicator["parse_args"](argv)


FAST = RetryPolicy(max_attempts_per_mode=2, initial_backoff_seconds=0.0, max_backoff_seconds=0.0, jitter_ratio=0.0)


def router_with(script: list[object]) -> tuple[LLMRouter, ScriptedProvider]:
    provider = ScriptedProvider("deepseek", script)
    router = LLMRouter(
        {"deepseek": provider},
        RoutingConfig(primary=LLMTarget("deepseek", "deepseek-chat"), retry=FAST),
        sleep=lambda _seconds: None,
    )
    return router, provider


def adjudication_reply(index: int, value: str, sided_with: str, *, timestamp: str = "00:01:01") -> str:
    return json.dumps({"resolutions": [{"index": index, "value": value, "timestamp": timestamp, "reason": "adjudged from the excerpt", "sided_with": sided_with, "confidence": 0.9}]})


FEEDBACK_REPLY = json.dumps({
    "keep_start_stop": {"keep": "merged keep", "start": "merged start", "stop": "merged stop"},
    "overall_summary": "merged summary",
})


def final_and_record(paths: dict[str, Path]) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        json.loads(paths["final"].read_text(encoding="utf-8")),
        json.loads(paths["record"].read_text(encoding="utf-8")),
    )


def test_only_the_dispute_reaches_the_model_and_its_verdict_maps_back_to_a_marker(tmp_path: Path, adjudicator, capsys) -> None:
    paths = write_inputs(tmp_path, {
        "nvidia__nemotron": marker_sheet(["Yes", "Yes", "Yes"], provider="nvidia", model="nemotron"),
        "gemini__pro": marker_sheet(["Yes", "No", "Yes"], provider="gemini", model="pro"),
    })
    # The adjudicator's letters follow the shuffled order; find which letter
    # gemini got for criterion 2 so the reply sides with it deliberately.
    order = adjudicator["reconciliation"].shuffled_order("s1:1", 2)
    gemini_letter = adjudicator["marker_letter"](order.index(1))
    router, provider = router_with([adjudication_reply(2, "No", gemini_letter), FEEDBACK_REPLY])

    assert adjudicator["run_panel"](args_for(adjudicator, paths, "nvidia__nemotron", "gemini__pro"), router) == 0

    final, record = final_and_record(paths)
    assert [item["value"] for item in final["criteria"]] == ["Yes", "No", "Yes"]
    assert final["marking_mode"] == "panel"
    assert final["model_provider"] == "panel"
    assert final["model"] == "panel(nvidia:nemotron + gemini:pro -> deepseek:deepseek-chat)"
    assert final["keep_start_stop"] == {"keep": "merged keep", "start": "merged start", "stop": "merged stop"}
    assert final["overall_summary"] == "merged summary"
    assert final["scoring_summary"]["yes_count"] == 2
    assert final["scoring_summary"]["pass_fail"] == "Pass"

    panel = final["panel"]
    assert panel["agreement"] == {
        "total": 3, "agreed": 2, "disputed": 1, "percent": 0.6667,
        "cohen_kappa": 0.0,  # one constant marker: observed equals expected agreement
        "pass_fail_agreed": True, "disputed_indexes": [1],
    }
    assert panel["criteria"][0]["resolution"] == "agreed"
    assert panel["criteria"][1]["resolution"] == "adjudicated"
    assert panel["criteria"][1]["sided_with"] == 1, "the letter must map back to gemini, position 1"
    assert panel["criteria"][1]["confidence"] == 0.9
    assert panel["adjudicator"]["called"] is True
    assert panel["adjudicator"]["ok"] is True
    assert panel["adjudicator"]["provider_id"] == "deepseek"
    assert panel["adjudicator"]["feedback_source"] == "merged"
    assert [marker["key"] for marker in panel["markers"]] == ["nvidia__nemotron", "gemini__pro"]
    assert panel["degraded"] is None

    # Two calls: one adjudication, one feedback merge — and the adjudication
    # prompt named only criterion 2.
    assert len(provider.calls) == 2
    assert [dispute["index"] for dispute in record["disputes"]] == [1]
    assert record["adjudication"]["ok"] is True
    assert record["resolutions"][0]["index"] == 1
    # The transcript window around gemini's cited moment reached the prompt.
    positions = record["disputes"][0]["positions"]
    gemini_position = next(item for item in positions if item["marker_key"] == "gemini__pro")
    assert "penicillin" in gemini_position["evidence"]
    assert "Saved:" in capsys.readouterr().err


def test_no_disputes_means_no_adjudication_call(tmp_path: Path, adjudicator) -> None:
    same = ["Yes", "No", "Yes"]
    paths = write_inputs(tmp_path, {
        "a__m": marker_sheet(same, provider="a", model="m"),
        "b__m": marker_sheet(same, provider="b", model="m"),
    })
    router, provider = router_with([FEEDBACK_REPLY])

    adjudicator["run_panel"](args_for(adjudicator, paths, "a__m", "b__m"), router)

    final, record = final_and_record(paths)
    assert [call[0] for call in provider.calls] == ["deepseek-chat"], "only the feedback merge ran"
    assert final["panel"]["agreement"]["disputed"] == 0
    assert final["panel"]["adjudicator"]["called"] is False
    assert record["disputes"] == []
    assert all(item["resolution"] == "agreed" for item in final["panel"]["criteria"])


def test_a_bad_reply_is_repaired_before_it_is_believed(tmp_path: Path, adjudicator) -> None:
    paths = write_inputs(tmp_path, {
        "a__m": marker_sheet(["Yes", "Yes", "Yes"], provider="a", model="m"),
        "b__m": marker_sheet(["Yes", "No", "Yes"], provider="b", model="m"),
    })
    # First reply answers the wrong criterion, second is right.
    router, provider = router_with([
        adjudication_reply(3, "No", "A"),
        adjudication_reply(2, "Yes", "neither"),
        FEEDBACK_REPLY,
    ])

    adjudicator["run_panel"](args_for(adjudicator, paths, "a__m", "b__m"), router)

    final, record = final_and_record(paths)
    assert final["criteria"][1]["value"] == "Yes"
    assert final["panel"]["criteria"][1]["resolution"] == "adjudicated"
    assert final["panel"]["criteria"][1]["sided_with"] is None
    assert record["adjudication"]["repair_attempts"] == 1
    assert len(provider.calls) == 3


def test_a_dead_adjudicator_falls_to_the_tie_break_per_criterion(tmp_path: Path, adjudicator) -> None:
    paths = write_inputs(tmp_path, {
        "a__m": marker_sheet(["Yes", "Yes", "No"], provider="a", model="m"),
        "b__m": marker_sheet(["No", "Yes", "Yes"], provider="b", model="m"),
    })
    dead = [LLMTransportError("503") for _ in range(12)]
    router, _provider = router_with(dead)

    adjudicator["run_panel"](args_for(adjudicator, paths, "a__m", "b__m", tie_break="strict"), router)

    final, record = final_and_record(paths)
    assert [item["value"] for item in final["criteria"]] == ["No", "Yes", "No"]
    assert final["panel"]["criteria"][0]["resolution"] == "tie_break:strict"
    assert final["panel"]["criteria"][2]["resolution"] == "tie_break:strict"
    assert final["panel"]["adjudicator"]["called"] is True
    assert final["panel"]["adjudicator"]["ok"] is False
    assert final["panel"]["tie_break"] == "strict"
    # Feedback could not be merged either: the closest marker's notes are used
    # and the sheet says whose.
    assert final["panel"]["adjudicator"]["feedback_source"] in {"marker:a__m", "marker:b__m"}
    assert final["keep_start_stop"]["keep"] == "m keep"
    assert any("tie-break" in warning for warning in final["warnings"])
    assert any("could not be merged" in warning for warning in final["warnings"])
    assert record["adjudication"]["error"]


def test_without_adjudicator_calls_no_model_and_records_the_configured_warnings(tmp_path: Path, adjudicator) -> None:
    paths = write_inputs(tmp_path, {
        "a__m": marker_sheet(["Yes", "Yes", "No"], provider="a", model="m"),
        "b__m": marker_sheet(["Yes", "No", "No"], provider="b", model="m"),
    })

    adjudicator["run_panel"](
        args_for(adjudicator, paths, "a__m", "b__m", warning=["Adjudicator deepseek has no API key here."]),
        None,
    )

    final, _record = final_and_record(paths)
    assert final["criteria"][1]["value"] == "Yes", "lenient tie-break awards the criterion"
    assert final["panel"]["criteria"][1]["resolution"] == "tie_break:lenient"
    assert final["panel"]["adjudicator"]["called"] is False
    assert final["panel"]["adjudicator"]["provider_id"] == ""
    assert final["panel"]["adjudicator"]["model"] == ""
    assert final["model"] == "panel(a:m + b:m)"
    assert "Adjudicator deepseek has no API key here." in final["panel"]["warnings"]


def test_a_sheet_from_another_rubric_fails_the_run_instead_of_being_reconciled(tmp_path: Path, adjudicator) -> None:
    wrong = marker_sheet(["Yes", "Yes", "Yes"], provider="b", model="m")
    wrong["criteria"][1]["label"] = "Asks about smoking"
    paths = write_inputs(tmp_path, {
        "a__m": marker_sheet(["Yes", "Yes", "Yes"], provider="a", model="m"),
        "b__m": wrong,
    })
    router, _provider = router_with([])

    with pytest.raises(adjudicator["reconciliation"].SheetAlignmentError, match="Asks about smoking"):
        adjudicator["run_panel"](args_for(adjudicator, paths, "a__m", "b__m"), router)
    assert not paths["final"].exists()


def test_fewer_than_two_markers_is_a_usage_error(tmp_path: Path, adjudicator) -> None:
    paths = write_inputs(tmp_path, {"a__m": marker_sheet(["Yes", "Yes", "Yes"], provider="a", model="m")})
    with pytest.raises(adjudicator["InputError"], match="at least 2 marker sheets"):
        adjudicator["run_panel"](args_for(adjudicator, paths, "a__m"), None)


def test_a_crash_between_calls_resumes_from_the_checkpointed_replies(tmp_path: Path, adjudicator, monkeypatch) -> None:
    paths = write_inputs(tmp_path, {
        "a__m": marker_sheet(["Yes", "Yes", "Yes"], provider="a", model="m"),
        "b__m": marker_sheet(["Yes", "No", "Yes"], provider="b", model="m"),
    })
    checkpoint = paths["final"].with_name(".s1.json.checkpoint.json")

    # First run: both model calls answer and are checkpointed, then the process
    # dies writing the record — simulated by failing that one write. runpy
    # returns a copy of the module namespace, so the patch goes on the real one.
    module_globals = adjudicator["run_panel"].__globals__
    real_write = module_globals["write_text_atomic"]

    def crash_on_record(path: Path, text: str) -> None:
        if path == paths["record"]:
            raise OSError("simulated crash")
        real_write(path, text)

    monkeypatch.setitem(module_globals, "write_text_atomic", crash_on_record)
    router, provider = router_with([adjudication_reply(2, "No", "A"), FEEDBACK_REPLY])
    with pytest.raises(OSError, match="simulated crash"):
        adjudicator["run_panel"](args_for(adjudicator, paths, "a__m", "b__m"), router)
    assert len(provider.calls) == 2
    assert checkpoint.exists(), "both replies were saved before the crash"
    assert not paths["final"].exists()

    # Second run: a model that cannot answer anything usable. The saved replies
    # must be used instead of asking again.
    monkeypatch.setitem(module_globals, "write_text_atomic", real_write)
    router2, provider2 = router_with([LLMTransportError("down")] * 12)
    assert adjudicator["run_panel"](args_for(adjudicator, paths, "a__m", "b__m"), router2) == 0

    final, _record = final_and_record(paths)
    assert provider2.calls == [], "nothing was re-asked"
    assert final["criteria"][1]["value"] == "No"
    assert final["panel"]["criteria"][1]["resolution"] == "adjudicated"
    assert final["panel"]["adjudicator"]["feedback_source"] == "merged"
    assert final["keep_start_stop"]["keep"] == "merged keep"
    assert not checkpoint.exists(), "a completed run removes its checkpoint"
