"""What happens when the host cannot hold the model.

A Canary-Qwen run died on a 5 GB checkpoint with ``OSError: The paging file is
too small for this operation to complete. (os error 1455)`` — Windows refusing
the commit charge while safetensors staged the weights in host memory. Three
things were wrong beyond the machine: the weights were read into host memory
even for a CUDA run, the failure surfaced as a hundred-line traceback, and the
job queue treated it as transient and spent every remaining attempt reproducing
it.

These tests pin the three fixes: load onto the target device and degrade to CPU,
classify exhaustion as a typed non-retryable error with an operator-readable
message, and let the router finish the session on the default engine.
"""
from __future__ import annotations

import asyncio
import importlib.util
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings
from app.core.exceptions import AppError, EmptyTranscriptError, TranscriptionResourceError
from app.pipeline.media import MediaPipeline
from app.pipeline.transcription.base import (
    EngineAvailability,
    TranscriptionRequest,
    TranscriptionResult,
)
from app.pipeline.transcription.canary_qwen_engine import (
    INSUFFICIENT_MEMORY_TOKEN,
    CanaryQwenEngine,
    is_memory_exhaustion,
    memory_failure_message,
)
from app.pipeline.transcription.diarization import PyannoteDiarizer
from app.services.job_queue_service import is_retryable_failure

from tests.test_canary_engine import FakeEvents, FakeRunner, transcribe
from tests.test_transcription_diarization import stub_script_root
from tests.test_transcription_router import RecordingEngine, StubAppSettings, build_router

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "canary_qwen_transcribe.py"
_spec = importlib.util.spec_from_file_location("canary_qwen_transcribe", _SCRIPT)
canary_script = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(canary_script)

# The tail of the real traceback, verbatim: this exact text is what the fix has
# to recognise, and a paraphrase would let a wording change pass unnoticed.
PAGING_FILE_TRACEBACK = """Canary-Qwen transcription failed with exit code 1.
`torch_dtype` is deprecated! Use `dtype` instead!
Traceback (most recent call last):
  File "scripts/canary_qwen_transcribe.py", line 142, in run
    model = SALM.from_pretrained(args.model)
  File "safetensors/torch.py", line 359, in load_file
    with safe_open(filename, framework="pt", device=device, backend=backend) as f:
OSError: The paging file is too small for this operation to complete. (os error 1455)"""


# --- the subprocess: classification and device degradation -------------------


class FakeSalm:
    """Stands in for NeMo's SALM: fails on the devices it was told to fail on."""

    def __init__(self, failing_devices: set[str], accepts_map_location: bool = True) -> None:
        self.failing_devices = failing_devices
        self.accepts_map_location = accepts_map_location
        self.calls: list[dict[str, Any]] = []

        if accepts_map_location:

            def from_pretrained(model_id: str, map_location: str = "cpu") -> Any:
                self.calls.append({"model": model_id, "map_location": map_location})
                if map_location in self.failing_devices:
                    raise OSError("The paging file is too small for this operation to complete. (os error 1455)")
                return {"model": model_id, "device": map_location}

        else:

            def from_pretrained(model_id: str) -> Any:  # type: ignore[misc]
                self.calls.append({"model": model_id})
                if "cpu" in self.failing_devices:
                    raise OSError("The paging file is too small for this operation to complete. (os error 1455)")
                return {"model": model_id, "device": "cpu"}

        self.from_pretrained = from_pretrained


@pytest.mark.parametrize(
    "error",
    [
        OSError("The paging file is too small for this operation to complete. (os error 1455)"),
        RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB"),
        RuntimeError("[enforce fail at alloc_cpu.cpp:117] . DefaultCPUAllocator: not enough memory"),
        MemoryError(),
    ],
)
def test_allocation_failures_are_recognised_by_the_script(error: BaseException) -> None:
    assert canary_script.is_memory_exhaustion(error) is True


def test_a_windows_error_number_alone_is_enough() -> None:
    # A localised Windows build prints the message in another language; the
    # error number is the part that never changes.
    error = OSError(1455, "Le fichier de pagination est insuffisant")
    error.winerror = 1455
    assert canary_script.is_memory_exhaustion(error) is True


def test_ordinary_failures_are_not_mistaken_for_exhaustion() -> None:
    assert canary_script.is_memory_exhaustion(ValueError("bad audio")) is False
    assert canary_script.is_memory_exhaustion(FileNotFoundError("model.safetensors")) is False


def test_weights_load_straight_onto_the_target_device() -> None:
    # The root cause of the original crash: loading to host memory first and
    # copying to the GPU afterwards needs the host commit a CUDA run was meant
    # to avoid.
    salm = FakeSalm(failing_devices=set())

    model, device = canary_script.load_salm(salm, "nvidia/canary-qwen-2.5b", "cuda")

    assert device == "cuda"
    assert model["device"] == "cuda"
    assert salm.calls == [{"model": "nvidia/canary-qwen-2.5b", "map_location": "cuda"}]


def test_a_cuda_load_that_runs_out_of_memory_falls_back_to_cpu() -> None:
    salm = FakeSalm(failing_devices={"cuda"})

    model, device = canary_script.load_salm(salm, "nvidia/canary-qwen-2.5b", "cuda")

    assert device == "cpu"
    assert model["device"] == "cpu"
    assert [call["map_location"] for call in salm.calls] == ["cuda", "cpu"]


def test_a_host_that_cannot_hold_the_model_raises_insufficient_memory() -> None:
    salm = FakeSalm(failing_devices={"cuda", "cpu"})

    with pytest.raises(canary_script.InsufficientMemory, match="paging file"):
        canary_script.load_salm(salm, "nvidia/canary-qwen-2.5b", "cuda")


def test_a_cpu_request_is_not_retried_on_cpu() -> None:
    salm = FakeSalm(failing_devices={"cpu"})

    with pytest.raises(canary_script.InsufficientMemory):
        canary_script.load_salm(salm, "nvidia/canary-qwen-2.5b", "cpu")

    assert len(salm.calls) == 1


def test_a_loader_without_map_location_is_called_without_it() -> None:
    # An older NeMo would forward the unknown keyword to the model constructor,
    # where it fails as something that reads nothing like a loading problem.
    salm = FakeSalm(failing_devices=set(), accepts_map_location=False)

    _, device = canary_script.load_salm(salm, "nvidia/canary-qwen-2.5b", "cuda")

    assert device == "cuda"
    assert salm.calls == [{"model": "nvidia/canary-qwen-2.5b"}]


def test_non_memory_load_failures_keep_their_own_exception() -> None:
    class Broken:
        @staticmethod
        def from_pretrained(model_id: str, map_location: str = "cpu") -> Any:
            raise ValueError("checkpoint is corrupt")

    with pytest.raises(ValueError, match="corrupt"):
        canary_script.load_salm(Broken, "nvidia/canary-qwen-2.5b", "cuda")


def test_the_script_exits_with_the_memory_code_and_one_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(canary_script, "nemo_available", lambda: True)

    def explode(_args: Any) -> int:
        raise canary_script.InsufficientMemory("os error 1455")

    monkeypatch.setattr(canary_script, "run", explode)

    exit_code = canary_script.main(
        ["--audio", str(tmp_path / "a.wav"), "--output", str(tmp_path / "a.json")]
    )

    assert exit_code == canary_script.EXIT_INSUFFICIENT_MEMORY
    stderr = capsys.readouterr().err
    assert canary_script.INSUFFICIENT_MEMORY_TOKEN in stderr
    assert "Traceback" not in stderr


def test_exhaustion_outside_the_load_is_classified_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A decode window that runs out of VRAM is just as permanent on this host
    # as a checkpoint that will not load.
    monkeypatch.setattr(canary_script, "nemo_available", lambda: True)

    def explode(_args: Any) -> int:
        raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")

    monkeypatch.setattr(canary_script, "run", explode)

    exit_code = canary_script.main(
        ["--audio", str(tmp_path / "a.wav"), "--output", str(tmp_path / "a.json")]
    )

    assert exit_code == canary_script.EXIT_INSUFFICIENT_MEMORY
    assert canary_script.INSUFFICIENT_MEMORY_TOKEN in capsys.readouterr().err


def test_unrelated_script_failures_still_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(canary_script, "nemo_available", lambda: True)

    def explode(_args: Any) -> int:
        raise ValueError("bad audio")

    monkeypatch.setattr(canary_script, "run", explode)

    with pytest.raises(ValueError, match="bad audio"):
        canary_script.main(["--audio", str(tmp_path / "a.wav"), "--output", str(tmp_path / "a.json")])


def test_the_engine_and_the_script_agree_on_the_token() -> None:
    # The engine greps stderr for this string; a drift on either side would
    # silently restore the retry-until-exhausted behaviour.
    assert canary_script.INSUFFICIENT_MEMORY_TOKEN == INSUFFICIENT_MEMORY_TOKEN


# --- the engine: a typed failure instead of a traceback ----------------------


class ExplodingRunner(FakeRunner):
    """Runs everything normally except the Canary script, which fails."""

    def __init__(self, *args: Any, failure: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.failure = failure

    async def run(self, command: str, args: list[str], label: str, **kwargs: Any):
        if Path(args[0]).name == "canary_qwen_transcribe.py":
            self.calls.append((command, list(args)))
            raise RuntimeError(self.failure)
        return await super().run(command, args, label, **kwargs)


def build_with_failure(tmp_path: Path, failure: str):
    """The engine wired exactly as ``test_canary_engine.build`` wires it, with
    a runner whose Canary script fails the way the real one did."""
    settings = Settings(
        root_dir=stub_script_root(tmp_path),
        backend_root=tmp_path,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
    )
    runner = ExplodingRunner(settings, None, None, [], failure=failure)
    events = FakeEvents()
    auth = SimpleNamespace(runtime=SimpleNamespace(whisperx_hf_token="hf-token"))
    media = MediaPipeline(settings, runner, events, auth)
    diarizer = PyannoteDiarizer(settings, runner, events, auth)
    return CanaryQwenEngine(settings, runner, events, media, diarizer), runner, events


def test_a_paging_file_failure_becomes_a_typed_resource_error(tmp_path: Path) -> None:
    engine, _, _ = build_with_failure(tmp_path, PAGING_FILE_TRACEBACK)

    with pytest.raises(TranscriptionResourceError) as excinfo:
        transcribe(engine, tmp_path)

    message = excinfo.value.message
    assert "nvidia/canary-qwen-2.5b" in message
    assert "paging file" in message.lower()
    assert "WhisperX" in message
    # The operator reads this on the session card; the frames name nothing they
    # can act on.
    assert "Traceback" not in message
    assert "safetensors" not in message


def test_the_scripts_own_token_is_recognised(tmp_path: Path) -> None:
    engine, _, _ = build_with_failure(
        tmp_path,
        f"Canary-Qwen transcription failed with exit code 5.\n{INSUFFICIENT_MEMORY_TOKEN} os error 1455",
    )

    with pytest.raises(TranscriptionResourceError):
        transcribe(engine, tmp_path)


def test_an_ordinary_subprocess_failure_is_left_alone(tmp_path: Path) -> None:
    # Still a plain RuntimeError, so the queue still retries it: a killed
    # process or a transient CUDA driver error is worth another attempt.
    engine, _, _ = build_with_failure(tmp_path, "Canary-Qwen transcription failed with exit code 1.\nSyntaxError")

    with pytest.raises(RuntimeError) as excinfo:
        transcribe(engine, tmp_path)

    assert not isinstance(excinfo.value, AppError)


def test_the_message_keeps_the_line_that_diagnosed_it() -> None:
    message = memory_failure_message("nvidia/canary-qwen-2.5b", PAGING_FILE_TRACEBACK)

    assert "os error 1455" in message
    assert is_memory_exhaustion(PAGING_FILE_TRACEBACK) is True
    assert is_memory_exhaustion("ffprobe: no such file") is False


# --- the queue: a permanent failure is not retried ---------------------------


def test_a_resource_failure_is_never_retried() -> None:
    # Three attempts at a model the machine cannot hold cost three model loads
    # and delay the error the operator has to act on.
    assert is_retryable_failure(TranscriptionResourceError("no memory")) is False
    assert is_retryable_failure(EmptyTranscriptError("no speech")) is False
    # The default reading is unchanged: a 5xx is still worth another attempt.
    assert is_retryable_failure(AppError("upstream exploded", status_code=500)) is True
    assert is_retryable_failure(RuntimeError("ffmpeg died")) is True


def test_a_resource_failure_reports_itself_as_a_server_shortage() -> None:
    error = TranscriptionResourceError("no memory")

    assert error.status_code == 507
    assert error.retryable is False


# --- the router: the session finishes on another engine ----------------------


class ResourceStarvedEngine(RecordingEngine):
    """An engine this host cannot run."""

    descriptor = replace(RecordingEngine.descriptor, id="starved", label="Starved Engine")

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        raise TranscriptionResourceError("This machine does not have enough memory to load starved-model.")


class UnavailableEngine(RecordingEngine):
    descriptor = replace(RecordingEngine.descriptor, id="unavailable", label="Unavailable Engine")

    async def availability(self) -> EngineAvailability:
        return EngineAvailability(False, "not installed")


def run_router(tmp_path: Path, engines: dict[str, Any], selected: str, default_engine_id: str):
    router, events = build_router(
        tmp_path,
        StubAppSettings(selected, {"batchSize": 4}),
        transcription_engine=default_engine_id,
    )
    router.engines = engines
    session = {"id": "session-1", "corpus": {"terms": []}}
    audio_info = {"absolutePath": str(tmp_path / "session-1.mp3"), "fileName": "session-1.mp3"}
    return router, events, session, audio_info


def test_a_starved_engine_hands_the_run_to_the_default_engine(tmp_path: Path) -> None:
    fallback = RecordingEngine()
    router, events, session, audio_info = run_router(
        tmp_path,
        {"starved": ResourceStarvedEngine(), "recording": fallback},
        selected="starved",
        default_engine_id="recording",
    )

    result = asyncio.run(router.transcribe(session, audio_info))

    assert result.engine_id == "recording"
    assert result.metadata["fallbackFrom"] == "starved"
    assert "not have enough memory" in result.metadata["fallbackReason"]
    assert len(fallback.requests) == 1
    messages = [payload.get("message", "") for _sid, event, payload in events.items if event == "log"]
    assert any("could not run here" in message for message in messages)
    assert any("Falling back to Recording Engine" in message for message in messages)


def test_the_fallback_runs_with_its_own_saved_options(tmp_path: Path) -> None:
    # The stored bag is keyed per engine: the failed engine's tuning must not
    # follow the run onto a different engine.
    fallback = RecordingEngine()
    router, _, session, audio_info = run_router(
        tmp_path,
        {"starved": ResourceStarvedEngine(), "recording": fallback},
        selected="starved",
        default_engine_id="recording",
    )

    asyncio.run(router.transcribe(session, audio_info))

    assert fallback.requests[0].options == fallback.resolve_options(None)


def test_no_fallback_hop_when_the_default_engine_is_the_one_that_failed(tmp_path: Path) -> None:
    router, _, session, audio_info = run_router(
        tmp_path,
        {"starved": ResourceStarvedEngine()},
        selected="starved",
        default_engine_id="starved",
    )

    with pytest.raises(TranscriptionResourceError):
        asyncio.run(router.transcribe(session, audio_info))


def test_an_unavailable_fallback_surfaces_the_original_failure(tmp_path: Path) -> None:
    router, _, session, audio_info = run_router(
        tmp_path,
        {"starved": ResourceStarvedEngine(), "unavailable": UnavailableEngine()},
        selected="starved",
        default_engine_id="unavailable",
    )

    with pytest.raises(TranscriptionResourceError, match="enough memory"):
        asyncio.run(router.transcribe(session, audio_info))


def test_other_engine_failures_are_not_second_guessed(tmp_path: Path) -> None:
    # Only a host-capacity failure earns a second engine. A crash, a bad
    # checkpoint or a corrupt recording would fail identically on the fallback
    # and must reach the operator as itself.
    class CrashingEngine(RecordingEngine):
        async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
            raise RuntimeError("whisperx segfaulted")

    fallback = RecordingEngine()
    router, _, session, audio_info = run_router(
        tmp_path,
        {"crashing": CrashingEngine(), "recording": fallback},
        selected="crashing",
        default_engine_id="recording",
    )

    with pytest.raises(RuntimeError, match="segfaulted"):
        asyncio.run(router.transcribe(session, audio_info))

    assert fallback.requests == []
