"""The WhisperX engine adapter.

WhisperX behaviour itself is pinned by the CLI-invocation suites. What matters
here is the adapter: that operator options reach the CLI, that the deployment's
environment defaults still apply to everything the operator did not set, and
that the engine reports the capabilities the router relies on.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.pipeline.transcription.base import TranscriptionRequest
from app.pipeline.transcription.whisperx_engine import WhisperXEngine

from tests.test_whisperx_invocation import make_media


def build(tmp_path: Path, **setting_overrides: Any) -> tuple[WhisperXEngine, Any]:
    media, runner = make_media(tmp_path, **setting_overrides)
    return WhisperXEngine(media), runner


def transcribe(engine: WhisperXEngine, tmp_path: Path, **request_overrides: Any):
    mp3_path = tmp_path / "session-1.mp3"
    mp3_path.write_bytes(b"fake-mp3")
    request = TranscriptionRequest(
        session_id="session-1",
        audio_path=mp3_path,
        audio_file_name=mp3_path.name,
        output_dir=tmp_path / "out",
        language="en",
        options=engine.resolve_options(request_overrides.pop("options", None)),
        **request_overrides,
    )
    return asyncio.run(engine.transcribe(request))


def flag(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


def test_the_engine_declares_full_capabilities(tmp_path: Path) -> None:
    engine, _ = build(tmp_path)

    capabilities = engine.descriptor.capabilities
    assert capabilities.diarization and capabilities.word_timestamps
    assert capabilities.subtitles and capabilities.progress


def test_defaults_come_from_the_environment_settings(tmp_path: Path) -> None:
    engine, _ = build(tmp_path, whisperx_model="distil-large-v3", whisperx_batch_size=4)

    defaults = engine.default_options()

    assert defaults["model"] == "distil-large-v3"
    assert defaults["batchSize"] == 4


def test_an_unset_option_keeps_the_deployment_default(tmp_path: Path) -> None:
    engine, runner = build(tmp_path, whisperx_model="distil-large-v3")

    transcribe(engine, tmp_path)

    assert flag(runner.whisperx_args(), "--model") == "distil-large-v3"


def test_operator_options_reach_the_cli(tmp_path: Path) -> None:
    engine, runner = build(tmp_path)

    transcribe(
        engine,
        tmp_path,
        options={"model": "large-v3-turbo", "batchSize": 8, "chunkSize": 15, "computeType": "int8"},
    )

    args = runner.whisperx_args()
    assert flag(args, "--model") == "large-v3-turbo"
    assert flag(args, "--batch_size") == "8"
    assert flag(args, "--chunk_size") == "15"
    assert flag(args, "--compute_type") == "int8"


def test_request_speaker_bounds_win_over_the_engine_option(tmp_path: Path) -> None:
    # The station's cast is one source of truth for every engine; an engine
    # option must not quietly diverge from it.
    engine, runner = build(tmp_path)

    transcribe(engine, tmp_path, options={"minSpeakers": 1, "maxSpeakers": 5}, min_speakers=2, max_speakers=2)

    args = runner.whisperx_args()
    assert flag(args, "--min_speakers") == "2"
    assert flag(args, "--max_speakers") == "2"


def test_corpus_terms_become_hotwords(tmp_path: Path) -> None:
    engine, runner = build(tmp_path)

    transcribe(engine, tmp_path, corpus_terms=["paracetamol", "nasal block"])

    assert flag(runner.whisperx_args(), "--hotwords") == "paracetamol, nasal block"


def test_progress_callbacks_are_forwarded(tmp_path: Path) -> None:
    media, _ = make_media(tmp_path, whisperx_stdout=["Progress: 50.00%...", "Progress: 100.00%..."])
    engine = WhisperXEngine(media)
    reported: list[float] = []

    async def on_progress(percent: float) -> None:
        reported.append(percent)

    transcribe(engine, tmp_path, on_progress=on_progress)

    assert reported and reported == sorted(reported)


def test_the_result_reports_the_engine_and_model(tmp_path: Path) -> None:
    engine, _ = build(tmp_path)

    result = transcribe(engine, tmp_path, options={"model": "large-v3-turbo"})

    assert result.engine_id == "whisperx"
    assert result.model == "large-v3-turbo"
    # WhisperX diarises itself, so the router adds no second pass.
    assert result.diarized is True
    assert result.json_path is not None
    assert result.to_outputs()["jsonAbsolutePath"] == result.json_path


def test_an_empty_filter_chain_option_skips_the_ffmpeg_pass(tmp_path: Path) -> None:
    engine, runner = build(tmp_path)

    transcribe(engine, tmp_path, options={"audioFilters": ""})

    assert not any(command == "ffmpeg" for command, _, _ in runner.calls)
    assert runner.whisperx_args()[0].endswith("session-1.mp3")


def test_availability_reports_a_missing_binary(tmp_path: Path) -> None:
    engine, _ = build(tmp_path, whisperx_bin=" ")

    availability = asyncio.run(engine.availability())

    assert availability.available is False
    assert "WHISPERX_BIN" in availability.reason
