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

The same host later failed again, harder — ``exit code 3221225477`` with three
lines of NeMo telemetry and nothing else, three times over. Running the script
under ``faulthandler`` named the crash: ``Windows fatal exception: access
violation`` inside ``torch.nn.Linear.__init__`` while the Conformer encoder was
being built, with the machine's free commit measured at 0.02 GB. Four more
things were wrong, and the tests below pin each of them:

* SALM builds its parameters at float32 although the published checkpoint is
  entirely bfloat16, so the load asked the host for roughly twice the commit
  the model actually needs;
* ``map_location=<the GPU>`` never worked on this torch/safetensors build
  (``Attempted to access the data pointer on an invalid python storage``), so
  every CUDA run quietly paid for the load twice;
* the CPU retry began with the failed attempt's memory still charged, because
  keeping the exception kept the traceback that pinned the half-built model;
* NeMo re-raises the ``MemoryError`` from the vocabulary read as an empty
  ``ValueError``, so the classifier never saw the diagnosis, and a crash exit
  code was not classified at all — leaving a permanent failure looking
  retryable.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import weakref
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
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
    FATAL_EXIT_CODES,
    INSUFFICIENT_MEMORY_TOKEN,
    CanaryQwenEngine,
    is_memory_exhaustion,
    memory_failure_message,
    native_crash_message,
    native_crash_reason,
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
# Registered before it runs, as a real import would be: the script's dataclasses
# resolve their (postponed) annotations through sys.modules[__module__].
sys.modules.setdefault(_spec.name, canary_script)
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


def paging_file_error() -> OSError:
    return OSError("The paging file is too small for this operation to complete. (os error 1455)")


class FakeSalm:
    """Stands in for NeMo's SALM.

    ``failing_attempts`` leading loads raise the Windows commit refusal. It is
    the attempt that varies, not the arguments: the loader is now called
    identically every time, because the weights are always staged in host
    memory and what differs between the CUDA attempt and the CPU one is only
    where the model is moved afterwards.
    """

    def __init__(self, failing_attempts: int = 0, accepts_map_location: bool = True) -> None:
        self.failing_attempts = failing_attempts
        self.accepts_map_location = accepts_map_location
        self.calls: list[dict[str, Any]] = []

        if accepts_map_location:

            def from_pretrained(model_id: str, map_location: str = "cpu") -> Any:
                self.calls.append({"model": model_id, "map_location": map_location})
                if len(self.calls) <= self.failing_attempts:
                    raise paging_file_error()
                return {"model": model_id, "map_location": map_location}

        else:

            def from_pretrained(model_id: str) -> Any:  # type: ignore[misc]
                self.calls.append({"model": model_id})
                if len(self.calls) <= self.failing_attempts:
                    raise paging_file_error()
                return {"model": model_id}

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


def raise_wrapped(inner: BaseException, outer: BaseException) -> BaseException:
    """``outer`` raised while ``inner`` was being handled, links and all."""
    try:
        raise inner
    except BaseException:  # noqa: BLE001 - re-raising is the point
        try:
            raise outer
        except BaseException as wrapped:  # noqa: BLE001
            return wrapped


def test_a_memory_error_wrapped_by_nemo_is_still_recognised() -> None:
    # Verbatim from the failing run. NeMo catches the MemoryError raised while
    # reading the Qwen vocabulary and re-raises its own summary — whose tail is
    # empty, because MemoryError carries no message. Reading only that summary,
    # the script concluded the checkpoint was broken.
    wrapped = raise_wrapped(
        MemoryError(),
        ValueError("Unable to instantiate HuggingFace AUTOTOKENIZER for Qwen/Qwen3-1.7B. Exception: "),
    )

    assert canary_script.is_memory_exhaustion(wrapped) is True


def test_the_chain_is_followed_through_an_explicit_raise_from() -> None:
    outer = RuntimeError("could not load the checkpoint")
    outer.__cause__ = paging_file_error()

    assert canary_script.is_memory_exhaustion(outer) is True


def test_a_wrapped_ordinary_failure_is_still_not_exhaustion() -> None:
    wrapped = raise_wrapped(FileNotFoundError("config.json"), ValueError("Unable to instantiate"))

    assert canary_script.is_memory_exhaustion(wrapped) is False


def test_a_cyclic_exception_chain_terminates() -> None:
    first = ValueError("first")
    second = ValueError("second")
    first.__context__ = second
    second.__context__ = first

    assert canary_script.is_memory_exhaustion(first) is False


# --- the subprocess: the precision the checkpoint is actually stored in ------


def test_auto_builds_the_model_in_the_checkpoints_precision() -> None:
    # nvidia/canary-qwen-2.5b ships 1686 bfloat16 tensors and no float32 ones.
    # Building at float32 asked the host to commit ~9 GB to hold ~4.6 GB of
    # values that were never wider, which is what put the load over the line.
    import torch

    assert canary_script.resolve_load_dtype("auto") is torch.bfloat16
    assert canary_script.resolve_load_dtype("float32") is torch.float32
    assert canary_script.resolve_load_dtype("float16") is torch.float16


def test_an_unknown_dtype_is_rejected_by_name() -> None:
    with pytest.raises(ValueError, match="float8"):
        canary_script.resolve_load_dtype("float8")


def test_the_dtype_flag_defaults_to_auto() -> None:
    args = canary_script.parse_args(["--audio", "a.wav", "--output", "a.json"])

    assert args.dtype == "auto"


def torch_nn() -> Any:
    import torch.nn

    return torch.nn


def install_fake_salm_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Put a stub where SALM's module lives, so the dtype patch is observable.

    The real package is a multi-second import of the whole NeMo stack, and half
    the point of the fix is that it touches exactly one name in exactly one
    module.
    """
    packages = [
        "nemo",
        "nemo.collections",
        "nemo.collections.speechlm2",
        "nemo.collections.speechlm2.models",
        "nemo.collections.speechlm2.models.salm",
    ]
    modules = [ModuleType(name) for name in packages]
    for parent, child, name in zip(modules, modules[1:], packages[1:]):
        setattr(parent, name.rsplit(".", 1)[-1], child)
    for name, module in zip(packages, modules):
        monkeypatch.setitem(sys.modules, name, module)

    salm_module = modules[-1]

    def load_pretrained_hf(model: str, dtype: Any = "float32") -> Any:
        return {"model": model, "dtype": dtype}

    salm_module.load_pretrained_hf = load_pretrained_hf
    return salm_module


def test_the_llm_backbone_is_built_in_the_requested_dtype(monkeypatch: pytest.MonkeyPatch) -> None:
    # torch's process-wide default covers the Conformer encoder, but the Qwen
    # backbone is built by load_pretrained_hf, whose own signature defaults to
    # float32 and so ignores it. NeMo exposes no config hook for this path.
    import torch

    salm_module = install_fake_salm_module(monkeypatch)
    original = salm_module.load_pretrained_hf

    with canary_script.built_in_dtype(torch.bfloat16):
        assert torch.get_default_dtype() is torch.bfloat16
        assert salm_module.load_pretrained_hf("Qwen/Qwen3-1.7B")["dtype"] is torch.bfloat16

    assert torch.get_default_dtype() is torch.float32
    assert salm_module.load_pretrained_hf is original


def test_the_defaults_are_restored_when_the_load_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # A leaked bfloat16 default would silently change every tensor the rest of
    # this interpreter builds, including the CPU retry's.
    import torch

    salm_module = install_fake_salm_module(monkeypatch)
    original = salm_module.load_pretrained_hf

    with pytest.raises(OSError):
        with canary_script.built_in_dtype(torch.bfloat16):
            raise paging_file_error()

    assert torch.get_default_dtype() is torch.float32
    assert salm_module.load_pretrained_hf is original


class FakePreprocessor(torch_nn().Module):
    """A mel front-end: computes in float32 and says so, whatever dtype it is in."""

    def __init__(self) -> None:
        super().__init__()
        import torch

        self.register_buffer("window", torch.ones(4, dtype=torch.bfloat16))

    def forward(self, _signal: Any) -> tuple[Any, Any]:
        import torch

        return torch.zeros(2, 3, dtype=torch.float32), torch.tensor([3, 3])


class FakeRelPositionAttention(torch_nn().Module):
    """NeMo's attention layer, in miniature: one Linear that honours the dtype
    default and two position biases that do not — ``torch.FloatTensor`` is
    float32 whatever ``torch.set_default_dtype`` says."""

    def __init__(self) -> None:
        super().__init__()
        import torch
        from torch import nn

        self.linear_pos = nn.Linear(4, 4, bias=False)
        self.pos_bias_u = nn.Parameter(torch.FloatTensor(2, 2))
        self.pos_bias_v = nn.Parameter(torch.FloatTensor(2, 2))


def build_bfloat16_salm() -> Any:
    """SALM's shape, built the way the script builds it: under a bfloat16 default."""
    import torch
    from torch import nn

    with canary_script.built_in_dtype(torch.bfloat16):
        model = nn.Module()
        model.perception = nn.Module()
        model.perception.preprocessor = FakePreprocessor()
        model.perception.encoder = nn.Module()
        model.perception.encoder.layers = nn.ModuleList([FakeRelPositionAttention(), FakeRelPositionAttention()])
        model.llm = nn.Module()
        model.llm.q_proj = nn.Linear(4, 4)
        # PEFT upcasts the LoRA adapters to float32 on purpose; the checkpoint
        # still holds them in bfloat16.
        model.llm.lora_A = nn.Linear(4, 2, bias=False, dtype=torch.float32)
        # transformers computes rotary positions in float32 and keeps the
        # table that way; a narrowed copy would not recover the precision.
        model.llm.register_buffer("inv_freq", torch.ones(2, dtype=torch.float32))
    return model


def test_the_mel_frontend_stays_in_float32_and_hands_on_the_load_dtype() -> None:
    # The preprocessor cannot follow the rest into bfloat16 — it runs an STFT
    # over the raw float32 waveform — but its float32 features are then rejected
    # by the encoder's first convolution: "Input type (float) and bias type
    # (struct c10::BFloat16) should be the same", raised after the whole model
    # has loaded. The boundary is cast instead of widened.
    import torch

    model = build_bfloat16_salm()

    alignment = canary_script.align_model_dtype(model, torch.bfloat16)

    assert alignment.frontends == ("perception.preprocessor",)
    assert model.perception.preprocessor.window.dtype is torch.float32
    features, lengths = model.perception.preprocessor(None)
    assert features.dtype is torch.bfloat16
    # Lengths are indices, not activations; casting them would corrupt them.
    assert lengths.dtype is torch.int64


def test_parameters_nemo_built_in_float32_are_brought_into_the_load_dtype() -> None:
    # The production failure: every Conformer attention layer creates its
    # relative-position biases with torch.FloatTensor, so they stayed float32
    # under the bfloat16 default and copy_ kept them that way. The first forward
    # then died with "expected scalar type Float but found BFloat16" where
    # q + pos_bias_v met linear_pos(pos_emb). The invariant is enforced after
    # the load, for every parameter, not patched per module.
    import torch

    model = build_bfloat16_salm()
    layer = model.perception.encoder.layers[0]
    assert layer.linear_pos.weight.dtype is torch.bfloat16
    assert layer.pos_bias_u.dtype is torch.float32, "the fixture must reproduce NeMo's hard-coded float32"

    alignment = canary_script.align_model_dtype(model, torch.bfloat16)

    assert layer.pos_bias_u.dtype is torch.bfloat16
    assert layer.pos_bias_v.dtype is torch.bfloat16
    assert model.llm.lora_A.weight.dtype is torch.bfloat16
    assert alignment.converted == (
        "perception.encoder.layers.0.pos_bias_u",
        "perception.encoder.layers.0.pos_bias_v",
        "perception.encoder.layers.1.pos_bias_u",
        "perception.encoder.layers.1.pos_bias_v",
        "llm.lora_A.weight",
    )
    # Every parameter outside the front-end now agrees, so no forward can meet
    # two dtypes in one matmul.
    stray = [
        name
        for name, parameter in model.named_parameters()
        if not name.startswith("perception.preprocessor.") and parameter.dtype is not torch.bfloat16
    ]
    assert stray == []


def test_buffers_are_left_in_the_precision_their_owner_chose() -> None:
    import torch

    model = build_bfloat16_salm()

    canary_script.align_model_dtype(model, torch.bfloat16)

    assert model.llm.inv_freq.dtype is torch.float32


def test_the_alignment_report_names_what_it_changed() -> None:
    import torch

    model = build_bfloat16_salm()

    report = canary_script.align_model_dtype(model, torch.bfloat16).describe()

    assert "front-end kept in float32 (perception.preprocessor)" in report
    assert "converted 5 parameter(s)" in report
    assert "perception.encoder.layers.0.pos_bias_u" in report
    assert "(5 in total)" in report


def test_a_model_already_in_the_load_dtype_reports_nothing_to_convert() -> None:
    import torch
    from torch import nn

    with canary_script.built_in_dtype(torch.bfloat16):
        model = nn.Linear(2, 2)

    report = canary_script.align_model_dtype(model, torch.bfloat16).describe()

    assert "nothing to convert" in report


def test_weights_are_staged_in_host_memory_even_for_a_cuda_run() -> None:
    # Asking safetensors for CUDA tensors reads like the way to keep the
    # checkpoint out of host memory, but the module tree is built on the host
    # regardless, and on this torch/safetensors build the request fails outright
    # with "Attempted to access the data pointer on an invalid python storage".
    # Every CUDA run therefore died and repeated the whole load on CPU. The move
    # to the device happens once, afterwards.
    salm = FakeSalm()

    model, device = canary_script.load_salm(salm, "nvidia/canary-qwen-2.5b", "cuda")

    assert device == "cuda"
    assert salm.calls == [{"model": "nvidia/canary-qwen-2.5b", "map_location": "cpu"}]
    assert model["map_location"] == "cpu"


def test_a_cuda_load_that_runs_out_of_memory_falls_back_to_cpu() -> None:
    salm = FakeSalm(failing_attempts=1)

    _model, device = canary_script.load_salm(salm, "nvidia/canary-qwen-2.5b", "cuda")

    assert device == "cpu"
    assert len(salm.calls) == 2


def test_the_failed_attempt_is_released_before_the_retry() -> None:
    # The CPU retry used to start with the failed attempt still charged:
    # ``last_error = error`` kept the exception, which kept its traceback, which
    # kept the frame that built the half-finished multi-gigabyte model. On a
    # host that was already at its commit limit the retry could only fail the
    # same way — or crash. Only the failure's text is kept now.
    class HalfBuiltModel:
        """Stands in for the several gigabytes a failed load leaves behind."""

    partial_ref: list[weakref.ReferenceType[Any]] = []
    collected_before_retry: list[bool] = []

    def from_pretrained(model_id: str, map_location: str = "cpu") -> Any:
        partial = HalfBuiltModel()  # pinned by this frame, and this frame by the traceback
        if not partial_ref:
            partial_ref.append(weakref.ref(partial))
            raise paging_file_error()
        collected_before_retry.append(partial_ref[0]() is None)
        return {"model": model_id}

    canary_script.load_salm(SimpleNamespace(from_pretrained=from_pretrained), "m", "cuda")

    assert collected_before_retry == [True]


def test_a_host_that_cannot_hold_the_model_raises_insufficient_memory() -> None:
    salm = FakeSalm(failing_attempts=2)

    with pytest.raises(canary_script.InsufficientMemory, match="paging file"):
        canary_script.load_salm(salm, "nvidia/canary-qwen-2.5b", "cuda")


def test_a_cpu_request_is_not_retried_on_cpu() -> None:
    salm = FakeSalm(failing_attempts=1)

    with pytest.raises(canary_script.InsufficientMemory):
        canary_script.load_salm(salm, "nvidia/canary-qwen-2.5b", "cpu")

    assert len(salm.calls) == 1


def test_a_loader_without_map_location_is_called_without_it() -> None:
    # An older NeMo would forward the unknown keyword to the model constructor,
    # where it fails as something that reads nothing like a loading problem.
    salm = FakeSalm(accepts_map_location=False)

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
    from app.core.resources import ResourceLease

    media = MediaPipeline(settings, runner, events, auth, gpu=ResourceLease.unbounded())
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


# --- the subprocess: the checkpoint is streamed, not duplicated --------------


def write_checkpoint(path: Path, tensors: dict[str, Any]) -> Path:
    from safetensors.torch import save_file

    save_file(tensors, str(path))
    return path


def test_the_checkpoint_is_copied_into_the_parameters_that_already_exist(tmp_path: Path) -> None:
    # huggingface_hub materialises the whole file and then calls
    # load_state_dict, so the host holds the model and a second full copy of its
    # weights at once — 4.6 GB plus 5.1 GB here. The parameters already exist;
    # only their values are missing.
    import torch
    from torch import nn

    model = nn.Linear(2, 3)
    checkpoint = write_checkpoint(
        tmp_path / "model.safetensors",
        {"weight": torch.full((3, 2), 7.0), "bias": torch.full((3,), -1.0)},
    )

    copied, missing = canary_script.copy_checkpoint_into(model, str(checkpoint))

    assert copied == 2
    assert missing == []
    assert torch.equal(model.weight.detach(), torch.full((3, 2), 7.0))
    assert torch.equal(model.bias.detach(), torch.full((3,), -1.0))


def test_a_name_the_checkpoint_does_not_carry_is_reported_not_raised(tmp_path: Path) -> None:
    # NeMo builds the backbone unweighted and expects the checkpoint to fill it,
    # so it loads non-strict; tied parameters share storage and are filled by
    # their partner's copy.
    import torch
    from torch import nn

    model = nn.Linear(2, 3)
    checkpoint = write_checkpoint(tmp_path / "model.safetensors", {"weight": torch.zeros(3, 2)})

    copied, missing = canary_script.copy_checkpoint_into(model, str(checkpoint))

    assert copied == 1
    assert missing == ["bias"]


def test_a_strict_load_still_rejects_a_mismatched_checkpoint(tmp_path: Path) -> None:
    import torch
    from torch import nn

    model = nn.Linear(2, 3)
    checkpoint = write_checkpoint(tmp_path / "model.safetensors", {"weight": torch.zeros(3, 2)})

    with pytest.raises(RuntimeError, match="bias"):
        canary_script.copy_checkpoint_into(model, str(checkpoint), strict=True)


def installed_loader() -> Any:
    """The hub's staging method as it is stored, not as attribute access binds it."""
    from huggingface_hub.hub_mixin import PyTorchModelHubMixin

    return PyTorchModelHubMixin.__dict__["_load_as_safetensor"]


def test_the_streaming_load_is_installed_and_removed() -> None:
    original = installed_loader()

    with canary_script.streamed_checkpoint_load():
        assert installed_loader() is not original

    assert installed_loader() is original


def test_the_streaming_load_is_removed_after_a_failure() -> None:
    # A leaked patch would change how every later load in this interpreter
    # applies its weights, including the CPU retry's.
    original = installed_loader()

    with pytest.raises(OSError):
        with canary_script.streamed_checkpoint_load():
            raise paging_file_error()

    assert installed_loader() is original


# --- the subprocess: refuse before spending a minute proving it --------------


def stub_memory(
    monkeypatch: pytest.MonkeyPatch, available: int | None, checkpoint: int | None
) -> None:
    monkeypatch.setattr(canary_script, "available_commit_bytes", lambda: available)
    monkeypatch.setattr(canary_script, "checkpoint_bytes", lambda _model: checkpoint)


def test_the_preflight_refuses_a_host_that_cannot_grant_the_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The load does not fail on such a host, it crashes: a refused commit
    # reaches torch as a buffer it writes to anyway. Refusing up front turns
    # that into a sentence, and hands the session to WhisperX ~40s earlier.
    stub_memory(monkeypatch, available=7 * 2**30, checkpoint=5 * 2**30)

    shortage = canary_script.preflight_memory("nvidia/canary-qwen-2.5b")

    assert shortage is not None
    assert "nvidia/canary-qwen-2.5b" in shortage
    assert "11.0 GB" in shortage and "7.0 GB" in shortage


def test_the_preflight_allows_a_host_with_headroom(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_memory(monkeypatch, available=24 * 2**30, checkpoint=5 * 2**30)

    assert canary_script.preflight_memory("nvidia/canary-qwen-2.5b") is None


@pytest.mark.parametrize(
    ("available", "checkpoint"),
    [(None, 5 * 2**30), (7 * 2**30, None), (None, None)],
)
def test_the_preflight_stays_out_of_the_way_when_it_cannot_measure(
    monkeypatch: pytest.MonkeyPatch, available: int | None, checkpoint: int | None
) -> None:
    # A guess here would block runs that would have worked — an uncached
    # checkpoint, a platform whose commit cannot be read.
    stub_memory(monkeypatch, available=available, checkpoint=checkpoint)

    assert canary_script.preflight_memory("nvidia/canary-qwen-2.5b") is None


def test_a_preflight_refusal_exits_with_the_memory_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # End to end through main(): the engine reads the token off stderr, so a
    # refusal has to reach it the same way a load failure does.
    monkeypatch.setattr(canary_script, "nemo_available", lambda: True)
    stub_memory(monkeypatch, available=7 * 2**30, checkpoint=5 * 2**30)
    monkeypatch.setattr(
        canary_script,
        "run",
        lambda args: (_ for _ in ()).throw(
            canary_script.InsufficientMemory(canary_script.preflight_memory(args.model) or "")
        ),
    )

    exit_code = canary_script.main(
        ["--audio", str(tmp_path / "a.wav"), "--output", str(tmp_path / "a.json")]
    )

    assert exit_code == canary_script.EXIT_INSUFFICIENT_MEMORY
    stderr = capsys.readouterr().err
    assert canary_script.INSUFFICIENT_MEMORY_TOKEN in stderr
    assert is_memory_exhaustion(stderr) is True


# --- the engine: a crash is a permanent failure, not a transient one ---------

# Verbatim from the failing run — everything the API had to go on. There is no
# traceback, no allocator message and no exhaustion wording anywhere in it: the
# interpreter was killed mid-instruction, so the only diagnosis is the number.
ACCESS_VIOLATION_FAILURE = """Canary-Qwen transcription failed with exit code 3221225477.
OneLogger: Setting error_handling_strategy to DISABLE_QUIETLY_AND_REPORT_METRIC_ERROR for rank (rank=0) with OneLogger disabled. To override: explicitly set error_handling_strategy parameter.
No exporters were provided. This means that no telemetry data will be collected.
`torch_dtype` is deprecated! Use `dtype` instead!"""


def test_the_production_crash_code_is_recognised() -> None:
    reason = native_crash_reason(ACCESS_VIOLATION_FAILURE)

    assert reason is not None
    assert "0xC0000005" in reason


def test_a_signal_death_is_recognised_too() -> None:
    # The Linux equivalent of the same shortage: the OOM killer's SIGKILL is
    # reported as a negative exit code and is just as permanent.
    assert native_crash_reason("whisperx failed with exit code -9.") == FATAL_EXIT_CODES[-9]
    assert native_crash_reason("x failed with exit code -11.") == FATAL_EXIT_CODES[-11]


def test_an_ordinary_exit_code_is_not_a_crash() -> None:
    # A script that exited 1 ran its own handlers and said why; retrying it can
    # still help, so it must keep its plain RuntimeError and its attempts.
    assert native_crash_reason("Canary-Qwen transcription failed with exit code 1.\nSyntaxError") is None
    assert native_crash_reason("Canary-Qwen transcription failed with exit code 5.") is None
    assert native_crash_reason("no exit code here") is None


def test_a_native_crash_becomes_a_typed_resource_error(tmp_path: Path) -> None:
    # Before this the crash was a plain RuntimeError: the queue retried it three
    # times, each attempt a ~40s model load that died identically, and then
    # failed the session with three lines of NeMo telemetry.
    engine, _, _ = build_with_failure(tmp_path, ACCESS_VIOLATION_FAILURE)

    with pytest.raises(TranscriptionResourceError) as excinfo:
        transcribe(engine, tmp_path)

    message = excinfo.value.message
    assert "nvidia/canary-qwen-2.5b" in message
    assert "0xC0000005" in message
    assert "WhisperX" in message
    assert excinfo.value.retryable is False


def test_the_crash_message_names_the_code_it_was_given() -> None:
    message = native_crash_message("nvidia/canary-qwen-2.5b", FATAL_EXIT_CODES[3221225477])

    assert "nvidia/canary-qwen-2.5b" in message
    assert "access violation" in message
    assert "pagefile" in message


def test_a_crashed_run_is_never_retried() -> None:
    assert is_retryable_failure(TranscriptionResourceError(native_crash_message("m", "0xC0000005"))) is False


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
