"""What happens when the selected engine is not installed here.

A session failed at transcription with ``Canary-Qwen transcription failed with
exit code 3. The NeMo toolkit is not installed. Install it with: uv sync
--group canary`` — three times, because the failure was a plain
``RuntimeError`` the queue retried. Nothing about the recording was wrong: the
Settings screen still named Canary-Qwen, and a ``uv sync`` without
``--group canary`` had removed NeMo from the environment. Under pip the
optional engine survived every later install, because pip only adds; a uv sync
is exact, and removes whatever the requested groups do not name.

Two fixes, pinned here. The router asks the engine's own availability probe —
the one the settings screen already reads — *before* running it, and hands an
unavailable engine's run to the default engine the way it already does for a
host that is out of memory. And the Canary engine classifies the script's
"NeMo missing" exit code as a typed, non-retryable failure, so the case the
probe's cache misses is still not retried.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from app.core.exceptions import (
    HOST_CANNOT_RUN_ENGINE_ERRORS,
    AppError,
    TranscriptionEngineUnavailableError,
    TranscriptionResourceError,
)
from app.pipeline.transcription.base import EngineAvailability, TranscriptionRequest, TranscriptionResult
from app.pipeline.transcription.canary_qwen_engine import EXIT_NEMO_MISSING, not_installed_message
from app.services.job_queue_service import is_retryable_failure

from tests.test_canary_engine import transcribe
from tests.test_transcription_memory_failure import build_with_failure, canary_script, run_router
from tests.test_transcription_router import RecordingEngine

# The runner's message for the real failure, verbatim.
NEMO_MISSING_FAILURE = (
    "Canary-Qwen transcription failed with exit code 3.\n"
    "The NeMo toolkit is not installed. Install it with: uv sync --group canary"
)


class NotInstalledEngine(RecordingEngine):
    """An engine whose optional dependency group is not in this environment."""

    descriptor = replace(
        RecordingEngine.descriptor,
        id="not-installed",
        label="Not Installed Engine",
        requirements="Install it with: uv sync --group not-installed.",
    )

    async def availability(self) -> EngineAvailability:
        return EngineAvailability(False, "The toolkit is not installed in the backend environment.")

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        raise AssertionError("an engine that reports itself unavailable must not be run")


# --- the error class ---------------------------------------------------------


def test_an_unavailable_engine_is_never_retried() -> None:
    # The toolkit will be just as absent on the next attempt; each retry only
    # delays the sentence the operator has to act on.
    assert is_retryable_failure(TranscriptionEngineUnavailableError("not installed")) is False
    assert TranscriptionEngineUnavailableError("not installed").status_code == 503


def test_the_router_treats_both_host_failures_alike() -> None:
    assert TranscriptionResourceError in HOST_CANNOT_RUN_ENGINE_ERRORS
    assert TranscriptionEngineUnavailableError in HOST_CANNOT_RUN_ENGINE_ERRORS
    assert all(issubclass(error, AppError) for error in HOST_CANNOT_RUN_ENGINE_ERRORS)


# --- the router: the selected engine is asked before it is run ---------------


def test_an_engine_that_is_not_installed_hands_the_run_to_the_default_engine(tmp_path: Path) -> None:
    fallback = RecordingEngine()
    router, events, session, audio_info = run_router(
        tmp_path,
        {"not-installed": NotInstalledEngine(), "recording": fallback},
        selected="not-installed",
        default_engine_id="recording",
    )

    result = asyncio.run(router.transcribe(session, audio_info))

    assert result.engine_id == "recording"
    assert result.metadata["fallbackFrom"] == "not-installed"
    assert "not installed" in result.metadata["fallbackReason"]
    # The engine's own install instructions travel with the reason.
    assert "uv sync --group not-installed" in result.metadata["fallbackReason"]
    assert len(fallback.requests) == 1
    messages = [payload.get("message", "") for _sid, event, payload in events.items if event == "log"]
    assert any("could not run here" in message for message in messages)
    assert any("Falling back to Recording Engine" in message for message in messages)


def test_the_fallback_runs_with_its_own_saved_options(tmp_path: Path) -> None:
    fallback = RecordingEngine()
    router, _, session, audio_info = run_router(
        tmp_path,
        {"not-installed": NotInstalledEngine(), "recording": fallback},
        selected="not-installed",
        default_engine_id="recording",
    )

    asyncio.run(router.transcribe(session, audio_info))

    assert fallback.requests[0].options == fallback.resolve_options(None)


def test_no_fallback_when_the_default_engine_is_the_missing_one(tmp_path: Path) -> None:
    router, _, session, audio_info = run_router(
        tmp_path,
        {"not-installed": NotInstalledEngine()},
        selected="not-installed",
        default_engine_id="not-installed",
    )

    with pytest.raises(TranscriptionEngineUnavailableError) as excinfo:
        asyncio.run(router.transcribe(session, audio_info))

    message = excinfo.value.message
    assert "Not Installed Engine (not-installed)" in message
    assert "uv sync --group not-installed" in message
    assert "Settings" in message
    assert excinfo.value.retryable is False


def test_an_unavailable_fallback_surfaces_the_original_failure(tmp_path: Path) -> None:
    class AlsoMissing(NotInstalledEngine):
        descriptor = replace(NotInstalledEngine.descriptor, id="also-missing", label="Also Missing")

    router, _, session, audio_info = run_router(
        tmp_path,
        {"not-installed": NotInstalledEngine(), "also-missing": AlsoMissing()},
        selected="not-installed",
        default_engine_id="also-missing",
    )

    with pytest.raises(TranscriptionEngineUnavailableError, match="Not Installed Engine"):
        asyncio.run(router.transcribe(session, audio_info))


def test_an_available_engine_is_not_second_guessed(tmp_path: Path) -> None:
    # The probe is a gate, not a substitute for running: an engine that says it
    # can run is run, and its own failures keep their own class.
    engine = RecordingEngine()
    router, _, session, audio_info = run_router(
        tmp_path,
        {"recording": engine},
        selected="recording",
        default_engine_id="recording",
    )

    result = asyncio.run(router.transcribe(session, audio_info))

    assert result.engine_id == "recording"
    assert "fallbackFrom" not in result.metadata
    assert len(engine.requests) == 1


# --- the engine: the script's exit code is classified ------------------------


def test_the_scripts_nemo_missing_exit_code_becomes_a_typed_failure(tmp_path: Path) -> None:
    # The availability answer is cached for a minute, so a sync that removes
    # NeMo between the probe and the run reaches the subprocess. Its exit code
    # must still read as "not installed", not as a failure worth retrying.
    engine, _, _ = build_with_failure(tmp_path, NEMO_MISSING_FAILURE)

    with pytest.raises(TranscriptionEngineUnavailableError) as excinfo:
        transcribe(engine, tmp_path)

    message = excinfo.value.message
    assert "NeMo toolkit is not installed" in message
    assert "uv sync --group canary" in message
    assert "Settings" in message
    assert excinfo.value.retryable is False


def test_the_engine_and_the_script_agree_on_the_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(canary_script, "nemo_available", lambda: False)

    code = canary_script.main(["--audio", str(tmp_path / "a.wav"), "--output", str(tmp_path / "o.json")])

    assert code == EXIT_NEMO_MISSING
    assert "uv sync --group canary" in capsys.readouterr().err


def test_the_not_installed_message_names_the_fix() -> None:
    message = not_installed_message()

    assert "Canary-Qwen 2.5B (canary-qwen)" in message
    assert "uv sync --group canary" in message
    assert "removes it" in message


def test_other_exit_codes_are_still_left_alone(tmp_path: Path) -> None:
    engine, _, _ = build_with_failure(tmp_path, "Canary-Qwen transcription failed with exit code 1.\nSyntaxError")

    with pytest.raises(RuntimeError) as excinfo:
        transcribe(engine, tmp_path)

    assert not isinstance(excinfo.value, AppError)
