"""Panel marking in the pipeline: parallel markers, one adjudicator, honest
degradation, and a cache check that knows which mode produced a sheet.

The subprocesses are faked at the CommandRunner seam. The fake plays both
scripts: as the assessor it writes a sheet attributed to whatever model its
``OSCE_LLM_ROUTING`` names (or fails on cue); as the adjudicator it writes a
final sheet carrying a ``panel`` block. What is asserted is the contract the
API keeps around them — what is spawned, with which environment, in which
order, and what is written when a marker never answers.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.core.process import CommandResult
from app.llm.panel import MarkingMode, TieBreak
from app.llm.routing import ROUTING_ENV_VAR, LLMTarget, RoutingConfig
from app.pipeline.marking.base import MarkerAssignment, MarkingPlan
from app.pipeline.marking.fingerprint import SHEET_INPUTS_KEY, input_signature
from app.pipeline.marking.reconciliation import MARKING_MODE_PANEL
from app.pipeline.marking.sheets import final_sheet_needs_refresh, marker_sheet_needs_refresh
from app.pipeline.scoring import ScoringPipeline
from app.services.pipeline_service import PipelineService
from app.services.session_service import SessionService

from tests.test_pipeline_service import FakeEvents, FakeMedia, FakeSessions, build_settings

RUBRIC = [("Introduces self", True), ("Asks about allergies", False), ("Explains the plan", False)]


def routing_env(provider: str, model: str, key_var: str, key: str) -> dict[str, str]:
    env = RoutingConfig(primary=LLMTarget(provider, model)).to_env()
    env[key_var] = key
    return env


NVIDIA = MarkerAssignment(
    target=LLMTarget("nvidia", "nemotron"),
    key="nvidia__nemotron",
    llm_env=routing_env("nvidia", "nemotron", "NVIDIA_API_KEY", "nvidia-key"),
)
GEMINI = MarkerAssignment(
    target=LLMTarget("gemini", "gemini-pro"),
    key="gemini__gemini-pro",
    llm_env=routing_env("gemini", "gemini-pro", "GEMINI_API_KEY", "gemini-key"),
)
DEEPSEEK = MarkerAssignment(
    target=LLMTarget("deepseek", "deepseek-chat"),
    key="deepseek__deepseek-chat",
    llm_env=routing_env("deepseek", "deepseek-chat", "DEEPSEEK_API_KEY", "deepseek-key"),
)


def panel_plan(*, adjudicator: MarkerAssignment | None = DEEPSEEK, warnings: tuple[str, ...] = (), tie_break=TieBreak.LENIENT) -> MarkingPlan:
    return MarkingPlan(
        mode=MarkingMode.PANEL,
        selected_mode=MarkingMode.PANEL,
        single=RoutingConfig(primary=LLMTarget("nvidia", "nemotron")),
        single_env=NVIDIA.llm_env,
        markers=(NVIDIA, GEMINI),
        adjudicator=adjudicator,
        tie_break=tie_break,
        warnings=warnings,
    )


def valid_sheet(
    provider: str,
    model: str,
    values: tuple[str, ...] = ("Yes", "Yes", "No"),
    inputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    criteria = [
        {"label": label, "is_critical": critical, "value": value, "timestamp": "00:00:05", "reason": "because"}
        for (label, critical), value in zip(RUBRIC, values, strict=True)
    ]
    sheet = {
        "session_id": "s1",
        "rubric_file": "embedded_in_case_study_pdf",
        "rubric_source": "case.pdf#rubric-section",
        "criteria": criteria,
        "keep_start_stop": {"keep": "k", "start": "s", "stop": "t"},
        "overall_summary": "summary",
        "model": model,
        "model_provider": provider,
        "prompt_version": "content-marking-v1",
        "scoring_summary": {"total_criteria": 3, "yes_count": 2, "no_count": 1, "critical_total": 1, "critical_yes": 1, "critical_no": 0, "pass_fail": "Pass"},
    }
    if inputs is not None:
        sheet[SHEET_INPUTS_KEY] = dict(inputs)
    return sheet


def panel_sheet(marker_keys=("nvidia__nemotron", "gemini__gemini-pro"), *, adjudicator=("deepseek", "deepseek-chat"), degraded=None, tie_break="lenient", resolutions=("agreed", "adjudicated", "agreed")) -> dict[str, Any]:
    sheet = valid_sheet("panel", "panel(...)")
    sheet["marking_mode"] = MARKING_MODE_PANEL
    sheet["panel"] = {
        "schema": "content-panel-v1",
        "markers": [{"key": key} for key in marker_keys],
        "adjudicator": {"provider_id": adjudicator[0], "model": adjudicator[1], "called": True},
        "agreement": {"total": 3, "agreed": 2, "disputed": 1},
        "criteria": [{"index": index, "resolution": resolution} for index, resolution in enumerate(resolutions)],
        "tie_break": tie_break,
        "degraded": degraded,
        "warnings": [],
    }
    return sheet


class FakeScorerRunner:
    """Plays the assessor and the adjudicator scripts at the subprocess seam."""

    def __init__(self, *, fail_markers: set[str] = frozenset(), hold: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_markers = set(fail_markers)
        # When set, marker runs wait for each other before returning, which
        # proves they were in flight at the same time.
        self.hold = hold
        self._in_flight = 0
        self._both_started = asyncio.Event()

    @staticmethod
    def _flag(args: list[str], name: str) -> str:
        return args[args.index(name) + 1]

    @staticmethod
    def _flags(args: list[str], name: str) -> list[str]:
        return [args[index + 1] for index, item in enumerate(args) if item == name]

    async def run(self, command: str, args: list[str], label: str, **kwargs: Any) -> CommandResult:
        env = dict(kwargs.get("env") or {})
        call = {"script": Path(args[0]).name, "args": list(args), "label": label, "env": env}
        self.calls.append(call)
        output = Path(self._flag(args, "--output"))

        if call["script"] == "nvidia_osce_assessor.py":
            routing = RoutingConfig.from_json(env[ROUTING_ENV_VAR], default_provider_id="nvidia")
            provider, model = routing.primary.provider_id, routing.primary.model
            if self.hold:
                self._in_flight += 1
                if self._in_flight >= 2:
                    self._both_started.set()
                await asyncio.wait_for(self._both_started.wait(), timeout=1)
            if provider in self.fail_markers:
                raise RuntimeError(f"{provider} is down")
            values = ("Yes", "Yes", "No") if provider == "nvidia" else ("Yes", "No", "No")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(valid_sheet(provider, model, values)), encoding="utf-8")
            return CommandResult(stdout="", stderr="")

        if call["script"] == "osce_panel_adjudicator.py":
            markers = [Path(item).stem for item in self._flags(args, "--marker")]
            without = "--without-adjudicator" in args
            sheet = panel_sheet(
                markers,
                adjudicator=("", "") if without else ("deepseek", "deepseek-chat"),
                tie_break=self._flag(args, "--tie-break"),
            )
            sheet["panel"]["warnings"] = self._flags(args, "--warning")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(sheet), encoding="utf-8")
            Path(self._flag(args, "--adjudication-output")).write_text("{}", encoding="utf-8")
            return CommandResult(stdout="", stderr="")

        raise AssertionError(f"unexpected script {call['script']}")

    def spawned(self, script: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["script"] == script]


class _Auth:
    class runtime:  # noqa: N801 - mirrors the attribute shape the runner reads
        nvidia_api_key = "nvidia-from-secrets"


class _PlanSettings:
    def __init__(self, plan: MarkingPlan) -> None:
        self.plan = plan

    async def marking_plan(self, owner_id: str | None = None) -> MarkingPlan:
        return self.plan


def build_pipeline(tmp_path: Path, runner: FakeScorerRunner, plan: MarkingPlan) -> ScoringPipeline:
    settings = Settings(root_dir=tmp_path, backend_root=tmp_path, ffmpeg_bin="f", ffprobe_bin="f", scorer_python_bin="py")
    settings.paths.output_scores_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "scripts").mkdir(exist_ok=True)
    for name in ("nvidia_osce_assessor.py", "osce_panel_adjudicator.py"):
        (tmp_path / "scripts" / name).write_text("# stub", encoding="utf-8")
    return ScoringPipeline(settings, runner, FakeEvents(), _Auth(), rubric_service=None, llm_settings=_PlanSettings(plan))  # type: ignore[arg-type]


def build_session(tmp_path: Path) -> dict[str, Any]:
    transcript = tmp_path / "transcripts" / "s1.json"
    transcript.parent.mkdir(exist_ok=True)
    transcript.write_text(json.dumps({"segments": [{"text": "hi", "start": 0, "end": 1}]}), encoding="utf-8")
    case_study = tmp_path / "case.pdf"
    case_study.write_bytes(b"%PDF-1.4")
    return {
        "id": "s1",
        "files": {"caseStudy": {"absolutePath": str(case_study)}},
        "outputs": {"transcript": {"absolutePath": str(transcript)}},
    }


def panel_dir(tmp_path: Path) -> Path:
    return tmp_path / "storage" / "output" / "scores" / "panel" / "s1"


def session_inputs(tmp_path: Path) -> dict[str, Any]:
    """The fingerprint of the transcript + case study ``build_session`` writes."""
    session = build_session(tmp_path)
    return input_signature(
        transcript_path=Path(session["outputs"]["transcript"]["absolutePath"]),
        case_study_path=Path(session["files"]["caseStudy"]["absolutePath"]),
    )


# --- execution -----------------------------------------------------------------


def test_markers_run_in_parallel_with_isolated_environments_then_the_adjudicator(tmp_path: Path) -> None:
    runner = FakeScorerRunner(hold=True)
    pipeline = build_pipeline(tmp_path, runner, panel_plan())
    progress: list[float] = []

    async def on_progress(percent: float) -> None:
        progress.append(percent)

    result = asyncio.run(pipeline.run_content_marking(build_session(tmp_path), panel_plan(), on_progress=on_progress))

    markers = runner.spawned("nvidia_osce_assessor.py")
    assert len(markers) == 2, "both markers were spawned (and, per the hold, were in flight together)"
    by_output = {Path(call["args"][call["args"].index("--output") + 1]).stem: call for call in markers}
    assert set(by_output) == {"nvidia__nemotron", "gemini__gemini-pro"}

    nvidia_env = by_output["nvidia__nemotron"]["env"]
    gemini_env = by_output["gemini__gemini-pro"]["env"]
    assert RoutingConfig.from_json(nvidia_env[ROUTING_ENV_VAR], default_provider_id="x").primary == LLMTarget("nvidia", "nemotron")
    assert RoutingConfig.from_json(gemini_env[ROUTING_ENV_VAR], default_provider_id="x").primary == LLMTarget("gemini", "gemini-pro")
    assert "GEMINI_API_KEY" not in nvidia_env, "a marker only carries its own key"
    assert gemini_env["GEMINI_API_KEY"] == "gemini-key"
    assert "DEEPSEEK_API_KEY" not in gemini_env
    assert by_output["gemini__gemini-pro"]["label"] == "Content scoring (gemini:gemini-pro)"

    [adjudicator] = runner.spawned("osce_panel_adjudicator.py")
    args = adjudicator["args"]
    assert sorted(Path(item).stem for item in runner._flags(args, "--marker")) == ["gemini__gemini-pro", "nvidia__nemotron"]
    assert Path(runner._flag(args, "--output")) == tmp_path / "storage" / "output" / "scores" / "s1.json"
    assert Path(runner._flag(args, "--adjudication-output")) == panel_dir(tmp_path) / "adjudication.json"
    assert runner._flag(args, "--tie-break") == "lenient"
    assert "--without-adjudicator" not in args
    assert adjudicator["env"]["DEEPSEEK_API_KEY"] == "deepseek-key"
    assert RoutingConfig.from_json(adjudicator["env"][ROUTING_ENV_VAR], default_provider_id="x").primary == LLMTarget("deepseek", "deepseek-chat")
    # The adjudicator runs after every marker finished.
    assert runner.calls.index(adjudicator) == 2

    assert result["url"] == "/media/scores/s1.json"
    final = json.loads(Path(result["absolutePath"]).read_text(encoding="utf-8"))
    assert final["marking_mode"] == "panel"
    assert progress == [40.0, 80.0, 100.0]
    # Where each marker's own sheet and the record are served from.
    assert result["panelArtifacts"]["markers"]["gemini__gemini-pro"]["url"] == "/media/scores/panel/s1/gemini__gemini-pro.json"
    assert result["panelArtifacts"]["adjudication"]["url"] == "/media/scores/panel/s1/adjudication.json"


def test_panel_artifacts_round_trip_through_the_public_session(tmp_path: Path) -> None:
    runner = FakeScorerRunner(hold=True)
    pipeline = build_pipeline(tmp_path, runner, panel_plan())

    result = asyncio.run(pipeline.run_content_marking(build_session(tmp_path), panel_plan()))

    public = SessionService.public_session({"id": "s1", "files": {}, "outputs": {"scores": result}})
    panel_artifacts = public["outputs"]["scores"]["panelArtifacts"]
    assert panel_artifacts["markers"]["gemini__gemini-pro"]["url"] == "/media/scores/panel/s1/gemini__gemini-pro.json"
    assert panel_artifacts["adjudication"]["url"] == "/media/scores/panel/s1/adjudication.json"
    assert panel_artifacts["markers"]["gemini__gemini-pro"]["fileName"] == "gemini__gemini-pro.json"
    assert panel_artifacts["adjudication"]["fileName"] == "adjudication.json"
    assert isinstance(panel_artifacts["markers"]["gemini__gemini-pro"]["sizeBytes"], int)
    assert panel_artifacts["markers"]["gemini__gemini-pro"]["sizeBytes"] > 0
    assert isinstance(panel_artifacts["adjudication"]["sizeBytes"], int)
    assert panel_artifacts["adjudication"]["sizeBytes"] > 0
    assert "absolutePath" not in json.dumps(public)


def test_a_marker_sheet_already_on_disk_is_reused_not_remarked(tmp_path: Path) -> None:
    runner = FakeScorerRunner()
    pipeline = build_pipeline(tmp_path, runner, panel_plan())
    session = build_session(tmp_path)
    inputs = session_inputs(tmp_path)
    panel_dir(tmp_path).mkdir(parents=True)
    (panel_dir(tmp_path) / "nvidia__nemotron.json").write_text(
        json.dumps(valid_sheet("nvidia", "nemotron", inputs=inputs)), encoding="utf-8"
    )
    # A stale sheet under gemini's key but written by another model is not reused.
    (panel_dir(tmp_path) / "gemini__gemini-pro.json").write_text(
        json.dumps(valid_sheet("gemini", "gemini-flash", inputs=inputs)), encoding="utf-8"
    )

    asyncio.run(pipeline.run_content_marking(session, panel_plan()))

    markers = runner.spawned("nvidia_osce_assessor.py")
    assert len(markers) == 1
    assert RoutingConfig.from_json(markers[0]["env"][ROUTING_ENV_VAR], default_provider_id="x").primary.provider_id == "gemini"
    assert len(runner.spawned("osce_panel_adjudicator.py")) == 1


def test_a_marker_sheet_marked_from_a_different_transcript_is_not_reused(tmp_path: Path) -> None:
    runner = FakeScorerRunner()
    pipeline = build_pipeline(tmp_path, runner, panel_plan())
    session = build_session(tmp_path)

    asyncio.run(pipeline.run_content_marking(session, panel_plan()))
    assert len(runner.spawned("nvidia_osce_assessor.py")) == 2

    transcript_path = Path(session["outputs"]["transcript"]["absolutePath"])
    transcript_path.write_text(
        json.dumps({"segments": [{"text": "a completely different recording", "start": 0, "end": 1}]}),
        encoding="utf-8",
    )

    asyncio.run(pipeline.run_content_marking(session, panel_plan()))

    assert len(runner.spawned("nvidia_osce_assessor.py")) == 4, "every marker re-ran against the new transcript"


def test_a_marker_sheet_marked_from_a_different_case_study_is_not_reused(tmp_path: Path) -> None:
    runner = FakeScorerRunner()
    pipeline = build_pipeline(tmp_path, runner, panel_plan())
    session = build_session(tmp_path)

    asyncio.run(pipeline.run_content_marking(session, panel_plan()))
    assert len(runner.spawned("nvidia_osce_assessor.py")) == 2

    case_study_path = Path(session["files"]["caseStudy"]["absolutePath"])
    case_study_path.write_bytes(b"%PDF-1.4\n%a different rubric entirely")

    asyncio.run(pipeline.run_content_marking(session, panel_plan()))

    assert len(runner.spawned("nvidia_osce_assessor.py")) == 4, "every marker re-ran against the new case study"


def test_a_marker_sheet_from_the_same_inputs_is_still_reused_across_a_restart(tmp_path: Path) -> None:
    runner = FakeScorerRunner()
    pipeline = build_pipeline(tmp_path, runner, panel_plan())
    session = build_session(tmp_path)

    asyncio.run(pipeline.run_content_marking(session, panel_plan()))
    assert len(runner.spawned("nvidia_osce_assessor.py")) == 2

    # Simulate a restart materialising the very same bytes at the very same
    # paths (e.g. GCS object cache re-population): content is unchanged.
    transcript_path = Path(session["outputs"]["transcript"]["absolutePath"])
    transcript_path.write_text(transcript_path.read_text(encoding="utf-8"), encoding="utf-8")
    case_study_path = Path(session["files"]["caseStudy"]["absolutePath"])
    case_study_path.write_bytes(case_study_path.read_bytes())

    asyncio.run(pipeline.run_content_marking(session, panel_plan()))

    assert len(runner.spawned("nvidia_osce_assessor.py")) == 2, "byte-identical inputs are still reused"


def test_a_marker_sheet_with_no_input_signature_is_refreshed(tmp_path: Path) -> None:
    runner = FakeScorerRunner()
    pipeline = build_pipeline(tmp_path, runner, panel_plan())
    session = build_session(tmp_path)
    panel_dir(tmp_path).mkdir(parents=True)
    # A sheet from a build before the fingerprint existed: well-formed and
    # attributed to the right model, but no input_signature key at all.
    (panel_dir(tmp_path) / "nvidia__nemotron.json").write_text(
        json.dumps(valid_sheet("nvidia", "nemotron")), encoding="utf-8"
    )

    asyncio.run(pipeline.run_content_marking(session, panel_plan()))

    markers = runner.spawned("nvidia_osce_assessor.py")
    assert len(markers) == 2, "both markers ran; the pre-fingerprint sheet was not trusted"


def test_each_marker_sheet_records_the_inputs_it_was_marked_from(tmp_path: Path) -> None:
    runner = FakeScorerRunner()
    pipeline = build_pipeline(tmp_path, runner, panel_plan())
    session = build_session(tmp_path)

    asyncio.run(pipeline.run_content_marking(session, panel_plan()))

    expected = session_inputs(tmp_path)
    for name in ("nvidia__nemotron.json", "gemini__gemini-pro.json"):
        payload = json.loads((panel_dir(tmp_path) / name).read_text(encoding="utf-8"))
        assert payload[SHEET_INPUTS_KEY] == expected


def test_one_marker_failing_degrades_to_the_survivor_and_a_rerun_retries_only_it(tmp_path: Path) -> None:
    runner = FakeScorerRunner(fail_markers={"gemini"})
    pipeline = build_pipeline(tmp_path, runner, panel_plan())

    result = asyncio.run(pipeline.run_content_marking(build_session(tmp_path), panel_plan()))

    assert runner.spawned("osce_panel_adjudicator.py") == [], "one sheet cannot be reconciled"
    assert result["panelArtifacts"]["adjudication"] is None
    assert list(result["panelArtifacts"]["markers"]) == ["nvidia__nemotron"]
    final = json.loads(Path(result["absolutePath"]).read_text(encoding="utf-8"))
    assert final["marking_mode"] == "panel"
    assert final["model_provider"] == "panel"
    assert final["criteria"] == valid_sheet("nvidia", "nemotron")["criteria"]
    assert final["panel"]["degraded"]["effective_mode"] == "single"
    assert final["panel"]["degraded"]["marker"] == "nvidia__nemotron"
    assert "gemini is down" in final["panel"]["degraded"]["reason"]
    assert any("gemini__gemini-pro failed" in warning for warning in final["panel"]["warnings"])
    assert final_sheet_needs_refresh(final, panel_plan()), "a degraded sheet is retried on the next run"

    # Second run: gemini recovers. Only gemini is re-marked; nvidia's sheet is
    # adopted and the adjudicator reconciles the two.
    runner.fail_markers.clear()
    asyncio.run(pipeline.run_content_marking(build_session(tmp_path), panel_plan()))
    later = runner.spawned("nvidia_osce_assessor.py")[2:]
    assert len(later) == 1
    assert RoutingConfig.from_json(later[0]["env"][ROUTING_ENV_VAR], default_provider_id="x").primary.provider_id == "gemini"
    assert len(runner.spawned("osce_panel_adjudicator.py")) == 1


def test_every_marker_failing_fails_the_step(tmp_path: Path) -> None:
    runner = FakeScorerRunner(fail_markers={"gemini", "nvidia"})
    pipeline = build_pipeline(tmp_path, runner, panel_plan())

    with pytest.raises(RuntimeError, match="Every panel marker failed") as excinfo:
        asyncio.run(pipeline.run_content_marking(build_session(tmp_path), panel_plan()))
    assert "nvidia is down" in str(excinfo.value)
    assert "gemini is down" in str(excinfo.value)
    assert not (tmp_path / "storage" / "output" / "scores" / "s1.json").exists()


def test_no_usable_adjudicator_runs_the_reconciliation_without_a_model(tmp_path: Path) -> None:
    runner = FakeScorerRunner()
    plan = panel_plan(adjudicator=None, warnings=("Adjudicator deepseek:deepseek-chat has no API key configured here.",), tie_break=TieBreak.STRICT)
    pipeline = build_pipeline(tmp_path, runner, plan)

    result = asyncio.run(pipeline.run_content_marking(build_session(tmp_path), plan))

    [adjudicator] = runner.spawned("osce_panel_adjudicator.py")
    assert "--without-adjudicator" in adjudicator["args"]
    assert runner._flag(adjudicator["args"], "--tie-break") == "strict"
    assert runner._flags(adjudicator["args"], "--warning") == ["Adjudicator deepseek:deepseek-chat has no API key configured here."]
    assert ROUTING_ENV_VAR not in adjudicator["env"], "no target is handed to a run that must not call one"
    final = json.loads(Path(result["absolutePath"]).read_text(encoding="utf-8"))
    assert final["panel"]["warnings"] == ["Adjudicator deepseek:deepseek-chat has no API key configured here."]


def test_a_single_mode_plan_still_runs_one_marker_to_the_final_path(tmp_path: Path) -> None:
    runner = FakeScorerRunner()
    plan = MarkingPlan.single_only(RoutingConfig(primary=LLMTarget("nvidia", "nemotron")), NVIDIA.llm_env)
    pipeline = build_pipeline(tmp_path, runner, plan)

    result = asyncio.run(pipeline.run_content_scoring(build_session(tmp_path)))

    assert [call["script"] for call in runner.calls] == ["nvidia_osce_assessor.py"]
    assert Path(result["absolutePath"]) == tmp_path / "storage" / "output" / "scores" / "s1.json"


# --- the cache predicate -------------------------------------------------------


def test_marker_sheet_reuse_requires_the_named_model(tmp_path: Path) -> None:
    inputs = session_inputs(tmp_path)
    other_inputs = dict(inputs)
    other_inputs["transcript"] = {"size_bytes": 0, "sha256": "0" * 64}

    assert not marker_sheet_needs_refresh(valid_sheet("nvidia", "nemotron", inputs=inputs), NVIDIA, inputs)
    assert marker_sheet_needs_refresh(valid_sheet("nvidia", "llama", inputs=inputs), NVIDIA, inputs), "another model's sheet"
    assert marker_sheet_needs_refresh(valid_sheet("openai", "nemotron", inputs=inputs), NVIDIA, inputs), "another provider's sheet"
    assert marker_sheet_needs_refresh({"criteria": []}, NVIDIA, inputs), "malformed"
    default_model = MarkerAssignment(target=LLMTarget("nvidia", ""), key="nvidia__default", llm_env={})
    assert not marker_sheet_needs_refresh(valid_sheet("nvidia", "whatever-default", inputs=inputs), default_model, inputs)

    # The fingerprint clause: matching signature reused, a different one or a
    # missing one refreshed, and no expected signature at all always refreshes.
    assert marker_sheet_needs_refresh(valid_sheet("nvidia", "nemotron", inputs=other_inputs), NVIDIA, inputs), "a different input signature"
    assert marker_sheet_needs_refresh(valid_sheet("nvidia", "nemotron"), NVIDIA, inputs), "no input signature recorded"
    assert marker_sheet_needs_refresh(valid_sheet("nvidia", "nemotron", inputs=inputs), NVIDIA, None), "inputs=None always refreshes"


def test_final_sheet_reuse_is_mode_aware() -> None:
    single_plan = MarkingPlan.single_only()
    plan = panel_plan()

    assert not final_sheet_needs_refresh(valid_sheet("nvidia", "nemotron"), single_plan)
    assert final_sheet_needs_refresh(panel_sheet(), single_plan), "a panel sheet under single mode is refreshed"
    assert final_sheet_needs_refresh(valid_sheet("nvidia", "nemotron"), plan), "a single sheet under panel mode is refreshed"
    assert not final_sheet_needs_refresh(panel_sheet(), plan)
    assert final_sheet_needs_refresh(panel_sheet(degraded={"reason": "x"}), plan)
    assert final_sheet_needs_refresh(panel_sheet(marker_keys=("nvidia__nemotron", "openai__gpt")), plan), "a swapped marker"
    assert final_sheet_needs_refresh(panel_sheet(adjudicator=("openai", "gpt")), plan), "a swapped adjudicator"
    # A tie-break change only matters when a tie-break decided something.
    tie_broken = panel_sheet(resolutions=("agreed", "tie_break:lenient", "agreed"))
    assert not final_sheet_needs_refresh(tie_broken, plan)
    assert final_sheet_needs_refresh(tie_broken, panel_plan(tie_break=TieBreak.STRICT))
    assert not final_sheet_needs_refresh(panel_sheet(), panel_plan(tie_break=TieBreak.STRICT))
    # No adjudicator can run here: an adjudicated sheet is still the better one.
    assert not final_sheet_needs_refresh(panel_sheet(), panel_plan(adjudicator=None))
    assert final_sheet_needs_refresh({"criteria": "nope"}, plan)


# --- the pipeline service seam -------------------------------------------------


class RecordingMarking:
    """A ScoringPipeline double that exposes the prepared-run seam."""

    def __init__(self, plan: MarkingPlan, *, refresh: bool) -> None:
        self.plan = plan
        self.refresh = refresh
        self.progress_seen: list[float] = []
        self.runs = 0

    async def prepare_content_marking(self, session: dict[str, Any]):
        double = self

        class Run:
            plan = double.plan

            def needs_refresh(self, payload: Any) -> bool:
                return double.refresh

            async def run(self, on_progress=None):
                double.runs += 1
                if on_progress is not None:
                    await on_progress(40.0)
                    double.progress_seen.append(40.0)
                return {"absolutePath": "scores.json", "payload": {"schema": "content-scoring-v1"}}

            def step_metadata(self) -> dict[str, Any]:
                return {"markingMode": str(double.plan.mode), "panel": {"agreement": {"agreed": 2, "total": 3}}}

        return Run()

    async def run_audio_professionalism(self, _session):  # pragma: no cover - disabled in these tests
        raise AssertionError

    async def run_communication_scoring(self, _session, _audio):  # pragma: no cover
        raise AssertionError


def test_pipeline_service_uses_the_prepared_run_and_records_its_metadata(tmp_path: Path) -> None:
    settings = build_settings(tmp_path, parallel_scoring=False, enable_audio_professionalism=False, enable_communication_scoring=False)
    session = {"id": "session-1", "files": {}, "outputs": {}}
    sessions = FakeSessions(session)
    scoring = RecordingMarking(panel_plan(), refresh=True)
    service = PipelineService(sessions=sessions, events=FakeEvents(), media=FakeMedia(settings), scoring=scoring)

    result = asyncio.run(service._run_cached_scoring_branches(session))

    assert result["scores"] == {"schema": "content-scoring-v1"}
    assert scoring.runs == 1
    step = sessions.current["pipeline"]["steps"]["content_scoring"]
    assert step["status"] == "completed"
    assert step["metadata"]["markingMode"] == "panel"
    assert step["metadata"]["panel"]["agreement"] == {"agreed": 2, "total": 3}
    assert step["metadata"]["reusedExistingArtifact"] is False
    # Progress reached the session document while the step ran.
    progress_writes = [write for write in sessions.writes if write.get("pipeline", {}).get("stepProgress") == 40.0]
    assert progress_writes, "the marker's progress was persisted"
    assert sessions.current["pipeline"]["stepProgress"] is None, "cleared when the step ended"


def test_pipeline_service_skips_the_run_when_the_prepared_predicate_accepts_the_sheet(tmp_path: Path) -> None:
    settings = build_settings(tmp_path, parallel_scoring=False, enable_audio_professionalism=False, enable_communication_scoring=False)
    sheet_path = tmp_path / "scores.json"
    sheet_path.write_text(json.dumps(panel_sheet()), encoding="utf-8")
    session = {"id": "session-1", "files": {}, "outputs": {"scores": {"absolutePath": str(sheet_path)}}}
    sessions = FakeSessions(session)
    scoring = RecordingMarking(panel_plan(), refresh=False)
    service = PipelineService(sessions=sessions, events=FakeEvents(), media=FakeMedia(settings), scoring=scoring)

    result = asyncio.run(service._run_cached_scoring_branches(session))

    assert scoring.runs == 0
    assert result["scores"]["marking_mode"] == "panel"
    step = sessions.current["pipeline"]["steps"]["content_scoring"]
    assert step["metadata"]["reusedExistingArtifact"] is True
