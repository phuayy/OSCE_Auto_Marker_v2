"""LLM fallback for the communication rubric parser.

The deterministic parser stays the first choice; the LLM engine
(``scripts/rubric_llm_fallback.py``) is mocked everywhere here — no network.
Also pins that a failed upload never corrupts the active rubric.
"""
from __future__ import annotations

import asyncio
import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import UploadFile

from app.core.config import Settings
from app.services.artifact_service import ArtifactService
from app.services.rubric_service import RubricParseError, RubricService
from tests.fixtures.rubric_versions import InMemoryRubricVersions

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REPO = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import parse_communication_rubric as parser  # noqa: E402
import rubric_llm_fallback as fallback  # noqa: E402

DEFAULT_PDFS = [
    REPO / "rubrics" / "PHR1012 OSCE Rubric.pdf",
    REPO / "PHR1012 OSCE Rubric.pdf",
]


def _text(count: int = 3, bullets: bool = True) -> str:
    parts = ["PHR1012 COMMUNICATION RUBRICS", "Communicative effectiveness"]
    for i in range(1, count + 1):
        parts.append(f"{i}. Criterion number {i}")
        if bullets:
            parts.append(f"• indicator a{i}")
            parts.append(f"• indicator b{i}")
    parts.append("Notes: All = 3, Most = 2, Some = 1, None = 0")
    return "\n".join(parts)


def _llm_extraction(criteria: list[dict[str, Any]], **kw: Any) -> fallback.LlmRubricExtraction:
    return fallback.LlmRubricExtraction(
        kind="communication",
        payload={"title": "New Rubric", "criteria": criteria, "notes": "n"},
        model=kw.get("model", "m-1"),
        provider=kw.get("provider", "p-1"),
    )


def _raise(exc: Exception):
    def _f(*a: Any, **k: Any):
        raise exc

    return _f


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    import env_loader

    monkeypatch.setattr(env_loader, "load_env_file", lambda *a, **k: None)


@pytest.fixture()
def no_llm(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []

    def _f(*a: Any, **k: Any):
        calls.append(a)
        raise AssertionError("LLM must not be called")

    monkeypatch.setattr(parser, "extract_rubric_with_llm", _f)
    return calls


# --- real default rubric ----------------------------------------------------


@pytest.mark.parametrize("pdf", DEFAULT_PDFS, ids=lambda p: str(p.relative_to(REPO)))
def test_default_rubric_passes_gate_without_llm(pdf: Path, no_llm: list[Any]) -> None:
    if not pdf.exists():
        pytest.skip("default rubric PDF absent")
    text = parser.read_pdf_text(pdf)
    payload = parser.build_payload(text)
    assert parser.rubric_quality_issues(payload) == []
    assert payload["extraction"] == {"method": "deterministic"}
    assert payload["criteria_count"] == 7
    assert payload["max_score"] == 21
    assert payload["pass_threshold"] == 11
    assert [c["id"] for c in payload["criteria"]] == list(range(1, 8))
    assert no_llm == []


# --- quality gate -----------------------------------------------------------


def _payload(criteria: list[dict[str, Any]]) -> dict[str, Any]:
    return {"criteria": criteria}


def _c(i: int, label: str = "L", ind: list[str] | None = None) -> dict[str, Any]:
    return {"id": i, "label": label, "indicators": ["x"] if ind is None else ind, "section": None}


def test_gate_accepts_good_payload() -> None:
    assert parser.rubric_quality_issues(_payload([_c(1), _c(2)])) == []


def test_gate_flags_no_indicators() -> None:
    issues = parser.rubric_quality_issues(_payload([_c(1, ind=[]), _c(2, ind=[])]))
    assert any("indicator" in i for i in issues)


def test_gate_flags_too_few_criteria() -> None:
    assert any("at least 2" in i for i in parser.rubric_quality_issues(_payload([_c(1)])))
    assert parser.rubric_quality_issues(_payload([]))


def test_gate_flags_non_contiguous_ids() -> None:
    assert any("contiguous" in i for i in parser.rubric_quality_issues(_payload([_c(1), _c(3)])))
    assert any("contiguous" in i for i in parser.rubric_quality_issues(_payload([_c(2), _c(3)])))


def test_gate_flags_duplicate_ids() -> None:
    assert any("duplicate" in i for i in parser.rubric_quality_issues(_payload([_c(1), _c(1)])))


def test_gate_flags_empty_label() -> None:
    assert any("empty label" in i for i in parser.rubric_quality_issues(_payload([_c(1, label="  "), _c(2)])))


# --- shared builder ---------------------------------------------------------


def test_deterministic_text_uses_shared_builder(no_llm: list[Any]) -> None:
    payload = parser.build_payload(_text(3))
    assert payload["schema"] == "communication-rubric-v1"
    assert payload["criteria_count"] == 3
    assert payload["max_score"] == 9
    assert payload["pass_threshold"] == math.ceil(9 / 2)
    assert payload["sections"] == [{"name": "Communicative effectiveness", "criteria_ids": [1, 2, 3]}]
    assert payload["extraction"] == {"method": "deterministic"}


def test_builder_pass_threshold_rule() -> None:
    seven = parser.build_rubric_payload(title="t", criteria=[_c(i) for i in range(1, 8)], notes="")
    assert (seven["max_score"], seven["pass_threshold"]) == (21, 11)
    four = parser.build_rubric_payload(title="t", criteria=[_c(i) for i in range(1, 5)], notes="")
    assert (four["max_score"], four["pass_threshold"]) == (12, 6)
    five = parser.build_rubric_payload(title="t", criteria=[_c(i) for i in range(1, 6)], notes="")
    assert five["pass_threshold"] == 8


# --- LLM path ---------------------------------------------------------------


def test_deterministic_error_uses_llm_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Any, ...]] = []
    criteria = [
        {"section": "communicative   EFFECTIVENESS", "label": "Opens well", "indicators": ["greets"]},
        {"section": None, "label": "Closes well", "indicators": ["summarises", " "]},
        {"section": "Custom section", "label": "Third", "indicators": []},
    ]

    def _fake(kind: str, text: str, **k: Any):
        seen.append((kind, text))
        return _llm_extraction(criteria)

    monkeypatch.setattr(parser, "extract_rubric_with_llm", _fake)
    payload = parser.build_payload("no numbered criteria here at all")
    assert seen == [("communication", "no numbered criteria here at all")]
    assert [c["id"] for c in payload["criteria"]] == [1, 2, 3]
    assert payload["criteria"][0]["section"] == "Communicative effectiveness"
    assert payload["criteria"][1]["section"] is None
    assert payload["criteria"][1]["indicators"] == ["summarises"]
    assert [s["name"] for s in payload["sections"]] == [
        "Communicative effectiveness",
        "Uncategorized",
        "Custom section",
    ]
    assert payload["title"] == "New Rubric"
    assert payload["max_score"] == 9 and payload["pass_threshold"] == 5
    ext = payload["extraction"]
    assert ext["method"] == "llm" and ext["model"] == "m-1" and ext["provider"] == "p-1"
    assert "Could not extract any numbered" in ext["deterministic_error"]


def test_llm_seven_criteria_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    crit = [{"section": None, "label": f"c{i}", "indicators": ["i"]} for i in range(7)]
    monkeypatch.setattr(parser, "extract_rubric_with_llm", lambda *a, **k: _llm_extraction(crit))
    payload = parser.build_payload("garbage")
    assert (payload["criteria_count"], payload["max_score"], payload["pass_threshold"]) == (7, 21, 11)


def test_gate_failure_uses_llm_and_lists_issues(monkeypatch: pytest.MonkeyPatch) -> None:
    crit = [{"section": None, "label": f"c{i}", "indicators": ["i"]} for i in range(3)]
    monkeypatch.setattr(parser, "extract_rubric_with_llm", lambda *a, **k: _llm_extraction(crit))
    payload = parser.build_payload(_text(3, bullets=False))  # parses, but no indicators
    assert payload["extraction"]["method"] == "llm"
    assert "no performance indicators" in payload["extraction"]["deterministic_error"]


def test_unavailable_after_raise_keeps_original_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        parser, "extract_rubric_with_llm", _raise(fallback.RubricLlmFallbackUnavailable("disabled"))
    )
    with pytest.raises(RuntimeError) as err:
        parser.build_payload("no criteria")
    assert str(err.value).startswith("Could not extract any numbered communication criteria")
    assert "disabled" not in str(err.value)


def test_unavailable_after_gate_failure_keeps_deterministic_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        parser, "extract_rubric_with_llm", _raise(fallback.RubricLlmFallbackUnavailable("disabled"))
    )
    payload = parser.build_payload(_text(3, bullets=False))
    assert payload["extraction"] == {"method": "deterministic"}
    assert payload["criteria_count"] == 3


def test_failed_after_gate_failure_keeps_deterministic_parse_with_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        parser, "extract_rubric_with_llm", _raise(fallback.RubricLlmFallbackFailed("provider down"))
    )
    payload = parser.build_payload(_text(3, bullets=False))
    assert payload["criteria_count"] == 3
    extraction = payload["extraction"]
    assert extraction["method"] == "deterministic"
    assert len(extraction["warnings"]) == 1 and "provider down" in extraction["warnings"][0]
    assert "no performance indicators" in extraction["warnings"][0]


def test_failed_reports_both_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        parser, "extract_rubric_with_llm", _raise(fallback.RubricLlmFallbackFailed("bad json"))
    )
    with pytest.raises(RuntimeError) as err:
        parser.build_payload("no criteria")
    message = str(err.value)
    assert "Could not extract any numbered" in message and "bad json" in message


def test_no_llm_flag(no_llm: list[Any]) -> None:
    with pytest.raises(RuntimeError, match="Could not extract any numbered"):
        parser.build_payload("no criteria", allow_llm=False)
    payload = parser.build_payload(_text(3, bullets=False), allow_llm=False)
    assert payload["extraction"] == {"method": "deterministic"}
    assert no_llm == []


# --- main() -----------------------------------------------------------------


def _run_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *extra: str) -> tuple[int | None, Path]:
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    out = tmp_path / "out.json"
    monkeypatch.setattr(sys, "argv", ["p", "--pdf", str(pdf), "--output", str(out), *extra])
    return parser.main(), out


def test_main_writes_output_with_extraction(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys, no_llm) -> None:
    monkeypatch.setattr(parser, "read_pdf_text", lambda p: _text(3))
    code, out = _run_main(monkeypatch, tmp_path)
    assert code == 0
    assert json.loads(out.read_text())["extraction"] == {"method": "deterministic"}
    assert "method: deterministic" in capsys.readouterr().err
    assert list(tmp_path.glob(".*.tmp")) == []


def test_main_unavailable_writes_no_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(parser, "read_pdf_text", lambda p: "nothing numbered")
    monkeypatch.setattr(
        parser, "extract_rubric_with_llm", _raise(fallback.RubricLlmFallbackUnavailable("off"))
    )
    with pytest.raises(RuntimeError, match="Could not extract any numbered"):
        _run_main(monkeypatch, tmp_path)
    assert not (tmp_path / "out.json").exists()


def test_main_failed_with_deterministic_parse_writes_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(parser, "read_pdf_text", lambda p: _text(3, bullets=False))
    monkeypatch.setattr(
        parser, "extract_rubric_with_llm", _raise(fallback.RubricLlmFallbackFailed("nope"))
    )
    code, out = _run_main(monkeypatch, tmp_path)
    assert code == 0
    written = json.loads(out.read_text())
    assert written["extraction"]["method"] == "deterministic" and written["extraction"]["warnings"]


def test_main_failed_writes_no_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(parser, "read_pdf_text", lambda p: "nothing numbered")
    monkeypatch.setattr(
        parser, "extract_rubric_with_llm", _raise(fallback.RubricLlmFallbackFailed("nope"))
    )
    with pytest.raises(RuntimeError, match="nope"):
        _run_main(monkeypatch, tmp_path)
    assert not (tmp_path / "out.json").exists()


def test_main_no_extractable_text_never_calls_llm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_llm) -> None:
    def _no_text(path: Path) -> str:
        raise RuntimeError("No extractable text found in PDF r.pdf.")

    monkeypatch.setattr(parser, "read_pdf_text", _no_text)
    with pytest.raises(RuntimeError, match="No extractable text"):
        _run_main(monkeypatch, tmp_path)
    assert no_llm == []


def test_main_no_llm_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_llm) -> None:
    monkeypatch.setattr(parser, "read_pdf_text", lambda p: "nothing numbered")
    with pytest.raises(RuntimeError, match="Could not extract any numbered"):
        _run_main(monkeypatch, tmp_path, "--no-llm-fallback")
    assert no_llm == []


# --- --force-llm and shared domain code -------------------------------------


def test_parser_uses_domain_functions() -> None:
    from app.domain import communication_rubric as domain

    assert parser.build_rubric_payload is domain.build_rubric_payload
    assert parser.rubric_quality_issues is domain.rubric_quality_issues
    assert parser.normalize_whitespace is domain.normalize_whitespace
    assert parser.canonicalize_section_name is domain.canonicalize_section_name


def test_force_llm_skips_deterministic_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUBRIC_LLM_FALLBACK", "true")
    monkeypatch.setattr(parser, "parse_rubric_text", _raise(AssertionError("parser must not run")))
    extraction = _llm_extraction([{"label": "Rapport", "indicators": ["Greets"], "section": None}])
    monkeypatch.setattr(parser, "extract_rubric_with_llm", lambda *a, **k: extraction)
    payload = parser.build_payload(_text(3), force_llm=True)
    assert payload["extraction"]["method"] == "llm"
    assert "deterministic_error" not in payload["extraction"]
    assert payload["criteria"][0]["label"] == "Rapport"


def test_force_llm_unavailable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUBRIC_LLM_FALLBACK", "true")
    monkeypatch.setattr(
        parser, "extract_rubric_with_llm", _raise(fallback.RubricLlmFallbackUnavailable("no key"))
    )
    with pytest.raises(RuntimeError, match="not available.*no key"):
        parser.build_payload(_text(3), force_llm=True)


def test_force_llm_failed_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUBRIC_LLM_FALLBACK", "true")
    monkeypatch.setattr(
        parser, "extract_rubric_with_llm", _raise(fallback.RubricLlmFallbackFailed("bad json"))
    )
    with pytest.raises(RuntimeError, match="AI extraction failed: bad json"):
        parser.build_payload(_text(3), force_llm=True)


def test_force_llm_refused_when_switch_off(monkeypatch: pytest.MonkeyPatch, no_llm) -> None:
    monkeypatch.setenv("RUBRIC_LLM_FALLBACK", "false")
    with pytest.raises(RuntimeError, match="turned off on this server"):
        parser.build_payload(_text(3), force_llm=True)
    assert no_llm == []


def test_cli_rejects_force_llm_with_no_llm_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys, "argv", ["p", "--pdf", "x.pdf", "--force-llm", "--no-llm-fallback"]
    )
    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args()
    assert exit_info.value.code == 2


def test_main_passes_force_llm_through(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def fake_build(text: str, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"extraction": {"method": "llm"}}

    monkeypatch.setattr(parser, "read_pdf_text", lambda p: _text(3))
    monkeypatch.setattr(parser, "build_payload", fake_build)
    code, _ = _run_main(monkeypatch, tmp_path, "--force-llm")
    assert code == 0
    assert seen["force_llm"] is True and seen["allow_llm"] is True


# --- RubricService ----------------------------------------------------------


class FakeRunner:
    """Stands in for the parser subprocess: records the call and writes the
    ``--output`` file with ``payload``, or raises ``error``."""

    def __init__(self, payload: dict[str, Any] | None = None, error: str | None = None) -> None:
        self.payload = payload or {"schema": "communication-rubric-v1", "criteria": [{"id": 1}], "title": "T"}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def run(self, command: str, args: list[str], label: str, *, env: dict[str, str] | None = None, **_: Any):
        self.calls.append({"args": args, "env": env})
        if self.error:
            raise RuntimeError(f"{label} failed with exit code 1.\nError: {self.error}")
        Path(args[args.index("--output") + 1]).write_text(json.dumps(self.payload), encoding="utf-8")


class FakeAssets:
    async def register_communication_rubric(self, *, path: Path, original_name: str, mime_type: str | None):
        return SimpleNamespace(
            asset={"id": "asset-1", "absolutePath": str(path), "contentSha256": "abc"},
            is_duplicate=False,
        )


class FakeLlmSettings:
    def __init__(self, env: dict[str, str] | None = None, fail: bool = False) -> None:
        self.env = env or {}
        self.fail = fail
        self.asked: list[Any] = []

    async def subprocess_env(self, user_id: str | None = None) -> dict[str, str]:
        self.asked.append(user_id)
        if self.fail:
            raise RuntimeError("no credentials")
        return dict(self.env)


def _service(tmp_path: Path, runner: FakeRunner, llm: Any = None, **settings_kw: Any) -> RubricService:
    # A private root (so storage is isolated) that still sees the real parser
    # script and the shipped default rubric.
    root = tmp_path / "root"
    root.mkdir()
    (root / "scripts").symlink_to(REPO / "scripts")
    if (REPO / "rubrics").exists():
        (root / "rubrics").symlink_to(REPO / "rubrics")
    settings = Settings(root_dir=root, backend_root=root, **settings_kw)
    settings.paths.ensure_layout()
    return RubricService(settings, runner, ArtifactService(settings), FakeAssets(), InMemoryRubricVersions(), llm_settings=llm)  # type: ignore[arg-type]


def _upload(tmp_path: Path, name: str = "new.pdf") -> UploadFile:
    f = (tmp_path / name)
    f.write_bytes(b"%PDF-1.4 new")
    return UploadFile(file=f.open("rb"), filename=name, headers={"content-type": "application/pdf"})


def test_run_parser_passes_routing_and_fallback_env(tmp_path: Path) -> None:
    runner = FakeRunner()
    llm = FakeLlmSettings({"OSCE_LLM_ROUTING": "{}", "OPENAI_API_KEY": "k"})
    svc = _service(tmp_path, runner, llm, rubric_llm_fallback=True)
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    asyncio.run(svc.run_parser(pdf))
    env = runner.calls[0]["env"]
    assert env["OSCE_LLM_ROUTING"] == "{}" and env["OPENAI_API_KEY"] == "k"
    assert env["RUBRIC_LLM_FALLBACK"] == "true"
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert llm.asked == [None]  # deployment default routing


def test_run_parser_forwards_disabled_fallback(tmp_path: Path) -> None:
    runner = FakeRunner()
    svc = _service(tmp_path, runner, FakeLlmSettings(), rubric_llm_fallback=False)
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    asyncio.run(svc.run_parser(pdf))
    assert runner.calls[0]["env"]["RUBRIC_LLM_FALLBACK"] == "false"


def test_routing_failure_is_swallowed(tmp_path: Path) -> None:
    runner = FakeRunner()
    svc = _service(tmp_path, runner, FakeLlmSettings(fail=True))
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    assert asyncio.run(svc.run_parser(pdf)) is not None
    assert "OSCE_LLM_ROUTING" not in runner.calls[0]["env"]


def test_no_llm_settings_attached_still_runs(tmp_path: Path) -> None:
    runner = FakeRunner()
    svc = _service(tmp_path, runner, None)
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    assert asyncio.run(svc.run_parser(pdf)) is not None


def _seed_active(svc: RubricService) -> None:
    paths = svc.settings.paths
    paths.auth_dir.mkdir(parents=True, exist_ok=True)
    paths.communication_rubric_pdf_path.write_bytes(b"OLD PDF")
    paths.communication_rubric_json_path.write_text(json.dumps({"title": "OLD"}), encoding="utf-8")


def test_failed_upload_leaves_previous_rubric_intact(tmp_path: Path) -> None:
    runner = FakeRunner(error="Could not extract any numbered communication criteria")
    svc = _service(tmp_path, runner)
    _seed_active(svc)
    with pytest.raises(RubricParseError) as err:
        asyncio.run(svc.upload(_upload(tmp_path)))
    assert str(err.value) == "Could not extract any numbered communication criteria"
    paths = svc.settings.paths
    assert paths.communication_rubric_pdf_path.read_bytes() == b"OLD PDF"
    assert json.loads(paths.communication_rubric_json_path.read_text())["title"] == "OLD"
    assert list(paths.auth_dir.glob(".communication_rubric.*.tmp")) == []
    assert list(paths.input_rubrics_dir.glob("*new.pdf")) == []


def test_successful_upload_swaps_pdf_and_json(tmp_path: Path) -> None:
    runner = FakeRunner(payload={"schema": "communication-rubric-v1", "title": "NEW", "criteria": [{"id": 1}]})
    svc = _service(tmp_path, runner)
    _seed_active(svc)
    result = asyncio.run(svc.upload(_upload(tmp_path)))
    paths = svc.settings.paths
    assert paths.communication_rubric_pdf_path.read_bytes() == b"%PDF-1.4 new"
    stored = json.loads(paths.communication_rubric_json_path.read_text())
    assert stored["title"] == "NEW" and stored["rubric_asset_id"] == "asset-1"
    assert result["rubric"]["title"] == "NEW"
    # The parser was pointed at a temp file, never the live JSON.
    out = runner.calls[0]["args"][runner.calls[0]["args"].index("--output") + 1]
    assert Path(out) != paths.communication_rubric_json_path
    assert list(paths.auth_dir.glob(".communication_rubric.*.tmp")) == []


def test_route_maps_parse_error_to_422() -> None:
    from fastapi import HTTPException

    from app.api.routes import rubrics as route

    class _Rubrics:
        async def upload(self, _, *, actor=None):
            raise RubricParseError("bad layout")

        async def reset(self, *, actor=None):
            raise RubricParseError("bad default")

    container = SimpleNamespace(rubrics=_Rubrics())
    request = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(HTTPException) as e1:
        asyncio.run(route.upload_communication_rubric(request, object(), container))  # type: ignore[arg-type]
    assert (e1.value.status_code, e1.value.detail) == (422, "bad layout")
    with pytest.raises(HTTPException) as e2:
        asyncio.run(route.reset_communication_rubric(request, container))  # type: ignore[arg-type]
    assert e2.value.status_code == 422


def test_reset_failure_keeps_current_rubric(tmp_path: Path) -> None:
    runner = FakeRunner(error="boom")
    svc = _service(tmp_path, runner)
    _seed_active(svc)
    if not svc.settings.paths.default_rubric_source_pdf.exists():
        pytest.skip("default rubric PDF absent")
    with pytest.raises(RubricParseError):
        asyncio.run(svc.reset())
    assert svc.settings.paths.communication_rubric_pdf_path.read_bytes() == b"OLD PDF"
    assert json.loads(svc.settings.paths.communication_rubric_json_path.read_text())["title"] == "OLD"


# --- communication scorer provenance ---------------------------------------


def test_communication_scorer_copies_rubric_extraction() -> None:
    import nvidia_osce_communication as scorer

    rubric = {
        "title": "R",
        "schema": "communication-rubric-v1",
        "max_score": 3,
        "pass_threshold": 2,
        "criteria": [{"id": 1, "label": "L", "indicators": []}],
    }
    payload = {
        "session_id": "s",
        "criteria": [
            {"id": 1, "score_label": "All", "evidence": "Said hello.", "timestamp": "00:00:05",
             "indicators_observed": []}
        ],
        "overall_summary": "ok",
    }
    plain, _ = scorer.validate_output(payload, rubric)
    assert "rubric_extraction" not in plain
    prov = {"method": "llm", "model": "m"}
    stamped, _ = scorer.validate_output(payload, {**rubric, "extraction": prov})
    assert stamped["rubric_extraction"] == prov
    assert "rubric_version" not in plain
    versioned, _ = scorer.validate_output(
        payload, {**rubric, "rubric_version": {"id": "v1", "number": 3}, "rubric_revision": "abc"}
    )
    assert versioned["rubric_version"] == {"id": "v1", "number": 3}
    assert versioned["rubric_revision"] == "abc"
