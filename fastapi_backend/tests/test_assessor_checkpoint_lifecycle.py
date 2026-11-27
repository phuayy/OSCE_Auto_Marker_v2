"""The content assessor's crash checkpoint resumes interrupted work only.

A run that spent every repair and still had issues is a terminal verdict; its
checkpoint must not be replayed by the queue's automatic retry (which used to
fail instantly, without calling the model).
"""
from __future__ import annotations

import json
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from scorer_checkpoint import CheckpointMessage, checkpoint_path_for_output, write_checkpoint  # noqa: E402

CRITERIA = [{"label": "Greets the patient", "is_critical": False}, {"label": "Checks allergies", "is_critical": False}]
GOOD = json.dumps(
    {
        "criteria": [
            {"value": "Yes", "timestamp": "00:00:10", "reason": "Said hello."},
            {"value": "No", "timestamp": "00:01:00", "reason": "Never asked."},
        ],
        "keep_start_stop": {
            "keep": "Keep greeting warmly.",
            "start": "Start asking about allergies.",
            "stop": "Stop interrupting the patient.",
        },
        "overall_summary": "A reasonable consultation.",
    }
)
BAD = json.dumps({"criteria": []})


def _message(content: str) -> CheckpointMessage:
    return CheckpointMessage(content, model="m", provider_id="p")


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    transcript = tmp_path / "t.json"
    transcript.write_text(json.dumps({"segments": [{"text": "hi"}]}), encoding="utf-8")
    case = tmp_path / "case.pdf"
    case.write_bytes(b"%PDF-1.4")
    output = tmp_path / "scores" / "s1.json"
    output.parent.mkdir()

    module = importlib.import_module("nvidia_osce_assessor")
    rubric = SimpleNamespace(
        criteria=CRITERIA, context_text="ctx", rubric_section_text="rub", method="deterministic",
        extraction={"method": "deterministic"}, source="cache",
    )
    monkeypatch.setattr(module, "build_router_from_env", lambda: object())
    monkeypatch.setattr(module, "describe_routing", lambda router: "test-routing")
    monkeypatch.setattr(module, "load_case_study_rubric", lambda *a, **k: rubric)
    monkeypatch.setattr(
        sys, "argv",
        ["assessor", "--transcript", str(transcript), "--case-study", str(case), "--output", str(output), "--session-id", "s1"],
    )
    calls: list[int] = []

    def script_model(replies: list[Any]):
        def fake(router, messages, expected_items=None):
            calls.append(1)
            reply = replies[len(calls) - 1]
            if isinstance(reply, Exception):
                raise reply
            return _message(reply)

        monkeypatch.setattr(module, "create_completion", fake)

    return SimpleNamespace(module=module, mp=monkeypatch, output=output, checkpoint=checkpoint_path_for_output(output), script_model=script_model, calls=calls)


def _run(h) -> int:
    return h.module.main()


def test_exhausted_repairs_clear_the_checkpoint_and_the_next_run_calls_the_model(harness, capsys):
    harness.script_model([BAD, BAD, BAD])
    with pytest.raises(RuntimeError, match="no usable output"):
        _run(harness)
    assert len(harness.calls) == 3
    assert not harness.checkpoint.exists()

    harness.calls.clear()
    harness.script_model([GOOD])
    assert _run(harness) == 0
    assert len(harness.calls) == 1
    assert harness.output.exists()
    assert not harness.checkpoint.exists()


def test_stale_checkpoint_at_the_repair_limit_starts_fresh(harness):
    # Capture the context a real run would use, then leave an exhausted checkpoint
    # behind as a pre-fix run would have.
    seen: dict[str, Any] = {}
    real_write = harness.module.write_checkpoint

    def spy(path, context, state):
        seen["context"] = context
        raise KeyboardInterrupt

    harness.mp.setattr(harness.module, "write_checkpoint", spy)
    harness.script_model([BAD])
    with pytest.raises(KeyboardInterrupt):
        _run(harness)
    harness.mp.setattr(harness.module, "write_checkpoint", real_write)
    write_checkpoint(
        harness.checkpoint, seen["context"],
        {"stage": "repair_completion_validated", "repair_attempts": 2, "last_message_content": BAD,
         "last_message_model": "m", "last_message_provider": "p",
         "normalized_payload": {"criteria": []}, "issues": ["Field 'criteria' must be a non-empty array."]},
        schema="nvidia-osce-assessor-checkpoint-v1",
    )
    harness.calls.clear()
    harness.script_model([GOOD])
    assert _run(harness) == 0
    assert len(harness.calls) == 1, "the model must be asked again, not the verdict replayed"


def test_crash_after_first_call_resumes_from_the_checkpoint(harness):
    real_write = harness.module.write_checkpoint
    state = {"n": 0}

    def crashing(path, context, st):
        real_write(path, context, st)
        state["n"] += 1
        if state["n"] == 1:  # first_completion_returned is on disk; die before validating
            raise KeyboardInterrupt

    harness.mp.setattr(harness.module, "write_checkpoint", crashing)
    harness.script_model([GOOD])
    with pytest.raises(KeyboardInterrupt):
        _run(harness)
    assert harness.checkpoint.exists()

    harness.mp.setattr(harness.module, "write_checkpoint", real_write)
    harness.calls.clear()
    harness.script_model([])
    assert _run(harness) == 0
    assert harness.calls == [], "resume must reuse the saved completion"
    assert not harness.checkpoint.exists()
    assert json.loads(harness.output.read_text())["criteria"][0]["value"] == "Yes"


def test_resume_mid_repair_still_repairs(harness):
    real_write = harness.module.write_checkpoint

    def crashing(path, context, st):
        real_write(path, context, st)
        if st.get("stage") == "repair_completion_returned":
            raise KeyboardInterrupt

    harness.mp.setattr(harness.module, "write_checkpoint", crashing)
    harness.script_model([BAD, GOOD])
    with pytest.raises(KeyboardInterrupt):
        _run(harness)
    assert harness.checkpoint.exists()

    harness.mp.setattr(harness.module, "write_checkpoint", real_write)
    harness.calls.clear()
    harness.script_model([])
    assert _run(harness) == 0
    assert harness.calls == []
