"""Scorer inputs are handed over, never guessed (F7).

The content scorer used to be started with a session id and a case study only,
and resolved the transcript itself — preferring the raw WhisperX ``.srt`` over
the normalised JSON the communication scorer reads, and falling back to the
newest PDF in the upload folder when the case study was missing. These tests pin
the API side of the fix: every scorer gets the normalised transcript explicitly,
and a missing input fails the step with a named path instead of a guess.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.process import CommandResult
from app.pipeline.scoring import ScoringPipeline


class RecordingRunner:
    def __init__(self, output_writer=None) -> None:
        self.calls: list[list[str]] = []
        self.output_writer = output_writer

    async def run(self, command: str, args: list[str], label: str, **_kwargs: Any) -> CommandResult:
        self.calls.append(list(args))
        if self.output_writer is not None:
            self.output_writer(args)
        return CommandResult(stdout="{}", stderr="")


class _Events:
    async def publish(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _Auth:
    class runtime:  # noqa: N801 - mirrors the attribute shape ScoringPipeline reads
        nvidia_api_key = ""


def _pipeline(tmp_path: Path, runner: RecordingRunner) -> ScoringPipeline:
    settings = Settings(root_dir=tmp_path, backend_root=tmp_path, ffmpeg_bin="f", ffprobe_bin="f", scorer_python_bin="p")
    settings.paths.output_scores_dir.mkdir(parents=True, exist_ok=True)
    # The scorer scripts live under the real repo; the pipeline only checks they exist.
    (tmp_path / "scripts").mkdir(exist_ok=True)
    for name in ("nvidia_osce_assessor.py", "nvidia_osce_communication.py", "audio_professionalism_extractor.py"):
        (tmp_path / "scripts" / name).write_text("# stub", encoding="utf-8")
    return ScoringPipeline(settings, runner, _Events(), _Auth(), rubric_service=None)  # type: ignore[arg-type]


def _flag(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


def test_content_scorer_is_given_the_normalised_transcript_and_the_case_study(tmp_path: Path) -> None:
    transcript = tmp_path / "transcripts" / "s1.json"
    transcript.parent.mkdir()
    transcript.write_text(json.dumps({"segments": [{"text": "hi"}]}), encoding="utf-8")
    case_study = tmp_path / "case.pdf"
    case_study.write_bytes(b"%PDF-1.4")

    def write_scores(args: list[str]) -> None:
        Path(_flag(args, "--output")).write_text(json.dumps({"criteria": []}), encoding="utf-8")

    runner = RecordingRunner(write_scores)
    pipeline = _pipeline(tmp_path, runner)
    session = {
        "id": "s1",
        "files": {"caseStudy": {"absolutePath": str(case_study)}},
        "outputs": {"transcript": {"absolutePath": str(transcript)}},
    }

    result = asyncio.run(pipeline.run_content_scoring(session))

    args = runner.calls[0]
    assert _flag(args, "--transcript") == str(transcript), "the scorer was left to find its own transcript"
    assert _flag(args, "--case-study") == str(case_study)
    assert _flag(args, "--session-id") == "s1"
    assert "payload" not in result
    assert json.loads(Path(result["absolutePath"]).read_text(encoding="utf-8")) == {"criteria": []}


@pytest.mark.parametrize(
    ("session", "missing"),
    [
        (
            {"id": "s1", "files": {"caseStudy": {"absolutePath": "CASE"}}, "outputs": {}},
            "normalised transcript",
        ),
        (
            {"id": "s1", "files": {}, "outputs": {"transcript": {"absolutePath": "TRANSCRIPT"}}},
            "case-study PDF",
        ),
    ],
)
def test_a_missing_input_fails_the_step_by_name_and_never_starts_the_scorer(
    tmp_path: Path, session: dict[str, Any], missing: str
) -> None:
    transcript = tmp_path / "s1.json"
    transcript.write_text("{}", encoding="utf-8")
    case_study = tmp_path / "case.pdf"
    case_study.write_bytes(b"%PDF-1.4")
    # Substitute the placeholders with real files so only the intended one is absent.
    for key, path in (("caseStudy", case_study),):
        if session["files"].get(key, {}).get("absolutePath") == "CASE":
            session["files"][key]["absolutePath"] = str(path)
    if session["outputs"].get("transcript", {}).get("absolutePath") == "TRANSCRIPT":
        session["outputs"]["transcript"]["absolutePath"] = str(transcript)

    runner = RecordingRunner()
    pipeline = _pipeline(tmp_path, runner)

    with pytest.raises(AppError) as excinfo:
        asyncio.run(pipeline.run_content_scoring(session))

    assert missing in str(excinfo.value)
    assert excinfo.value.status_code == 422
    assert excinfo.value.retryable is False, "an absent file is not a transient fault"
    assert runner.calls == [], "the scorer subprocess was started without its inputs"


def test_scorer_scripts_refuse_to_guess_inputs() -> None:
    """The three subprocesses no longer carry a directory-scanning fallback."""
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    for name in ("nvidia_osce_assessor.py", "nvidia_osce_communication.py", "audio_professionalism_extractor.py"):
        source = (scripts_dir / name).read_text(encoding="utf-8")
        assert "newest_file" not in source, f"{name} still scans a directory for its inputs"
        assert "session_id_from_latest_metadata" not in source, f"{name} still guesses the session"
        assert "from scorer_inputs import" in source, f"{name} does not use the shared input contract"
