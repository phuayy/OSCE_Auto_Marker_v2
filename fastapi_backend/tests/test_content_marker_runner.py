"""The content-marking seam: one marker subprocess, driven by a plan.

``ScoringPipeline.run_content_scoring`` used to build the assessor's command
line and environment inline. It now resolves a :class:`MarkingPlan` once and
hands the spawn to a strategy, and these tests pin what must not have moved in
that refactor: the arguments, the environment the plan contributes, and the
fallback when the settings service cannot answer.
"""
from __future__ import annotations

import asyncio
import json
import runpy
import sys
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.process import CommandResult
from app.pipeline.marking.base import MarkingPlan
from app.pipeline.scoring import ScoringPipeline

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


class EnvRecordingRunner:
    def __init__(self, write_output: bool = True) -> None:
        self.calls: list[dict[str, Any]] = []
        self.write_output = write_output

    async def run(self, command: str, args: list[str], label: str, **kwargs: Any) -> CommandResult:
        self.calls.append({"command": command, "args": list(args), "label": label, "env": dict(kwargs.get("env") or {})})
        if self.write_output:
            output = Path(args[args.index("--output") + 1])
            output.write_text(json.dumps({"criteria": [{"label": "x"}]}), encoding="utf-8")
            return CommandResult(stdout="", stderr="")
        return CommandResult(stdout=json.dumps({"criteria": [{"label": "from-stdout"}]}), stderr="")


class _Events:
    async def publish(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _Auth:
    class runtime:  # noqa: N801 - mirrors the attribute shape the runner reads
        nvidia_api_key = "nvidia-from-secrets"


class _PlanSettings:
    def __init__(self, plan: MarkingPlan | None = None, error: Exception | None = None) -> None:
        self.plan = plan
        self.error = error
        self.calls = 0

    async def marking_plan(self) -> MarkingPlan:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.plan is not None
        return self.plan


def _pipeline(tmp_path: Path, runner: Any, llm_settings: Any | None = None) -> ScoringPipeline:
    settings = Settings(root_dir=tmp_path, backend_root=tmp_path, ffmpeg_bin="f", ffprobe_bin="f", scorer_python_bin="py")
    settings.paths.output_scores_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "nvidia_osce_assessor.py").write_text("# stub", encoding="utf-8")
    return ScoringPipeline(settings, runner, _Events(), _Auth(), rubric_service=None, llm_settings=llm_settings)  # type: ignore[arg-type]


def _session(tmp_path: Path) -> dict[str, Any]:
    transcript = tmp_path / "transcripts" / "s1.json"
    transcript.parent.mkdir(exist_ok=True)
    transcript.write_text(json.dumps({"segments": [{"text": "hi"}]}), encoding="utf-8")
    case_study = tmp_path / "case.pdf"
    case_study.write_bytes(b"%PDF-1.4")
    return {
        "id": "s1",
        "files": {"caseStudy": {"absolutePath": str(case_study)}},
        "outputs": {"transcript": {"absolutePath": str(transcript)}},
    }


def test_the_plan_env_is_layered_over_the_python_env(tmp_path: Path) -> None:
    runner = EnvRecordingRunner()
    plan = MarkingPlan.single_only(llm_env={"OSCE_LLM_ROUTING": "{\"primary\":{}}", "GEMINI_API_KEY": "g"})
    pipeline = _pipeline(tmp_path, runner, _PlanSettings(plan))

    result = asyncio.run(pipeline.run_content_scoring(_session(tmp_path)))

    call = runner.calls[0]
    assert call["command"] == "py"
    assert call["label"] == "Content scoring"
    assert call["args"][0].endswith("nvidia_osce_assessor.py")
    assert call["env"]["OSCE_LLM_ROUTING"] == "{\"primary\":{}}"
    assert call["env"]["GEMINI_API_KEY"] == "g"
    # The in-memory NVIDIA key still travels, as it always has.
    assert call["env"]["NVIDIA_API_KEY"] == "nvidia-from-secrets"
    assert result["url"] == "/media/scores/s1.json"
    assert Path(result["absolutePath"]) == tmp_path / "storage" / "output" / "scores" / "s1.json"


def test_a_plan_that_cannot_be_resolved_falls_back_to_the_environment(tmp_path: Path) -> None:
    runner = EnvRecordingRunner()
    settings = _PlanSettings(error=RuntimeError("settings database unreachable"))
    pipeline = _pipeline(tmp_path, runner, settings)

    asyncio.run(pipeline.run_content_scoring(_session(tmp_path)))

    assert settings.calls == 1
    env = runner.calls[0]["env"]
    assert "OSCE_LLM_ROUTING" not in env, "a failed resolve must not invent a routing"
    assert env["NVIDIA_API_KEY"] == "nvidia-from-secrets"


def test_no_settings_service_means_the_legacy_environment(tmp_path: Path) -> None:
    runner = EnvRecordingRunner()
    pipeline = _pipeline(tmp_path, runner, llm_settings=None)

    plan = asyncio.run(pipeline.content_marking_plan())

    assert plan == MarkingPlan.single_only()
    asyncio.run(pipeline.run_content_scoring(_session(tmp_path)))
    assert "OSCE_LLM_ROUTING" not in runner.calls[0]["env"]


def test_stdout_json_is_persisted_when_the_script_writes_no_file(tmp_path: Path) -> None:
    runner = EnvRecordingRunner(write_output=False)
    pipeline = _pipeline(tmp_path, runner, _PlanSettings(MarkingPlan.single_only()))

    result = asyncio.run(pipeline.run_content_scoring(_session(tmp_path)))

    assert json.loads(Path(result["absolutePath"]).read_text(encoding="utf-8")) == {"criteria": [{"label": "from-stdout"}]}


def test_the_assessor_marks_with_the_shared_prompt_and_validator(monkeypatch) -> None:
    """The extraction of content_marking.py left the assessor using those very
    objects — not private copies that could drift from a panel marker's."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    for name in ("content_marking", "scorer_checkpoint", "nvidia_osce_assessor"):
        sys.modules.pop(name, None)
    assessor = runpy.run_path(str(SCRIPTS / "nvidia_osce_assessor.py"))
    shared = runpy.run_path(str(SCRIPTS / "content_marking.py"))

    for name in (
        "build_system_prompt", "build_user_prompt", "validate_output",
        "compute_scoring_summary", "extract_rubric_criteria_from_case_study_rubric",
        "build_follow_up_messages",
    ):
        assert assessor[name].__module__ == "content_marking", f"{name} is defined locally in the assessor again"
        assert name in shared
    assert assessor["PROMPT_VERSION"] == shared["PROMPT_VERSION"]
    assert "prompt_version" in (SCRIPTS / "nvidia_osce_assessor.py").read_text(encoding="utf-8")
