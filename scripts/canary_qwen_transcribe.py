"""Transcribe an audio file with NVIDIA Canary-Qwen (NeMo SALM).

Runs as a subprocess, like the three scoring models: NeMo pulls in a large
torch/lightning stack, and loading it inside the API process would slow every
request and tie the API's lifetime to a model's memory. Reads a 16 kHz mono
WAV, writes a JSON document of timed segments.

Canary-Qwen was trained on utterances of up to 40 seconds, so a consultation is
decoded in overlapping windows and each window's text becomes one segment. The
window boundaries are the segment boundaries — this model returns no internal
timestamps — which is precise enough for the diarisation merge and the subtitle
track that follow.

Usage:
    python scripts/canary_qwen_transcribe.py --audio in.wav --output out.json
    python scripts/canary_qwen_transcribe.py --check      # dependency probe
    python scripts/canary_qwen_transcribe.py --download   # cache the weights
"""
from __future__ import annotations

import argparse
import contextlib
import faulthandler
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# A checkpoint this size is loaded a byte away from the machine's commit limit,
# and when Windows refuses the charge torch's CPU allocator does not always get
# an error it can raise: it writes to the null buffer it was handed and the
# interpreter dies with an access violation, before any ``except`` runs. Without
# this the only evidence is exit code 3221225477 and whatever NeMo happened to
# print, which names nothing. With it the crashing Python stack is on stderr,
# which is how the ``torch.nn.Linear.__init__`` frame below was found at all.
faulthandler.enable()

SCHEMA = "canary-segments-v1"
TARGET_SAMPLE_RATE = 16000

# The checkpoint is bfloat16 on disk, but SALM builds its Qwen backbone and its
# Conformer encoder at NeMo's float32 default and only then overwrites them with
# the bfloat16 weights — so the host briefly holds ~9 GB of parameters to end up
# with ~4.6 GB of values that were never wider than bfloat16. On a machine whose
# free commit is around 10 GB that overshoot is the difference between a
# transcript and an access violation, and it buys no precision: bfloat16 is the
# precision the model was trained and published in.
DEFAULT_LOAD_DTYPE = "bfloat16"
LOAD_DTYPE_CHOICES = ("auto", "bfloat16", "float16", "float32")

# generate() defaults to a bare GenerationConfig (bos/eos/pad only) — greedy,
# with no repetition guard. On real speech-level audio (verified: RMS/peak
# consistent with speech in every window, not silence) that still collapsed
# into thousands of characters of one repeated CJK token, in every window of
# an affected clip. Whisper's CLI ships logprob/no-speech/compression decode
# guards for exactly this failure mode; SALM's generate has none, so they are
# supplied here instead of relying on greedy decoding to stay coherent.
DEFAULT_REPETITION_PENALTY = 1.3
DEFAULT_NO_REPEAT_NGRAM_SIZE = 4
# 512 was enough runway for a stuck decode to fill a whole window with
# repeated characters. A 40s-trained model transcribing a <=30s window needs a
# small fraction of that — roughly 150 tokens for continuous speech at a
# natural rate — so this bounds the damage of any repetition that still gets
# through rather than relying on repetition_penalty alone.
MAX_NEW_TOKENS_PER_WINDOW = 256

# Committable memory the load needs, as a multiple of the checkpoint's size on
# disk: one copy for the module tree the weights are streamed into, plus the
# interpreter, the CUDA context and the transient spikes of building a few
# thousand modules.
#
# Calibrated by measurement rather than arithmetic. canary-qwen-2.5b settles at
# ~6.2 GiB of process commit against a 4.8 GiB checkpoint, but loads attempted
# with 6.4, 7.7, 9.6 and 9.8 GiB of grantable commit all died anyway — the
# spikes are well above the plateau, and losing that race costs a crash rather
# than an error. 2.2x refuses those and clears comfortably on a host with an
# ordinary pagefile.
HEADROOM_RATIO = 2.2

# Exit codes: 2 bad arguments, 3 NeMo missing, 4 download failed, 5 the host
# could not give the checkpoint the memory it needs.
EXIT_INSUFFICIENT_MEMORY = 5
# The engine greps stderr for this token and turns the run into a typed,
# non-retryable failure instead of a 200-line traceback.
INSUFFICIENT_MEMORY_TOKEN = "canary-insufficient-memory:"

# Substrings every allocator failure this script must survive prints somewhere
# in its message. Windows reports a commit-limit refusal as OS error 1455 with
# the "paging file" wording; Linux raises ENOMEM; torch has its own two.
_MEMORY_ERROR_SIGNATURES = (
    "paging file is too small",
    "os error 1455",
    "winerror 1455",
    "cannot allocate memory",
    "not enough memory",
    "out of memory",
    "insufficient memory",
    "bad_alloc",
)


class InsufficientMemory(RuntimeError):
    """The checkpoint could not be loaded on this machine, on any device."""


def _exception_chain(error: BaseException, limit: int = 20) -> list[BaseException]:
    """``error`` and everything it was raised from or during, oldest last.

    Both links are followed. ``__cause__`` is an explicit ``raise ... from``;
    ``__context__`` is the exception that was already being handled — which is
    the one that matters here, because a library that catches a failure and
    re-raises its own summary sets only the implicit link.
    """
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(chain) < limit:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _is_memory_exhaustion_directly(error: BaseException) -> bool:
    if isinstance(error, MemoryError):
        return True
    if isinstance(error, OSError):
        if getattr(error, "winerror", None) == 1455 or error.errno == 12:
            return True
    text = f"{type(error).__name__}: {error}".lower()
    return any(signature in text for signature in _MEMORY_ERROR_SIGNATURES)


def is_memory_exhaustion(error: BaseException) -> bool:
    """Whether ``error`` is the host running out of memory rather than a bug.

    Matched by signature, not by type: safetensors surfaces the Windows commit
    limit as a bare ``OSError``, torch raises ``RuntimeError`` for both CUDA and
    CPU allocator failures, and the C++ allocator raises ``MemoryError``. What
    they share is the wording, so that is what is checked — plus the two error
    numbers that carry no wording at all (WinError 1455, ENOMEM).

    The whole chain is checked, not just the exception in hand, because the
    wording is routinely thrown away by the layer above. NeMo catches the
    ``MemoryError`` raised while reading the Qwen vocabulary and re-raises
    ``ValueError: Unable to instantiate HuggingFace AUTOTOKENIZER for
    Qwen/Qwen3-1.7B. Exception:`` — with an empty tail, because ``MemoryError``
    carries no message. Reading only that summary, the script concluded the
    checkpoint was broken and re-raised, losing both the diagnosis and the
    non-retryable exit code.
    """
    return any(_is_memory_exhaustion_directly(link) for link in _exception_chain(error))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Transcribe audio with NVIDIA Canary-Qwen.")
    parser.add_argument("--check", action="store_true", help="Report whether NeMo is importable, then exit.")
    parser.add_argument(
        "--download",
        action="store_true",
        help="Download the checkpoint into the HuggingFace cache, then exit. Already-cached files are reused.",
    )
    parser.add_argument("--audio", type=Path, help="Input audio file (16 kHz mono WAV).")
    parser.add_argument("--output", type=Path, help="Where to write the segments JSON.")
    parser.add_argument("--model", default="nvidia/canary-qwen-2.5b")
    parser.add_argument("--chunk-seconds", type=float, default=30.0)
    parser.add_argument("--overlap-seconds", type=float, default=2.0)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    parser.add_argument(
        "--dtype",
        default="auto",
        choices=list(LOAD_DTYPE_CHOICES),
        help=(
            "Precision the parameters are built in. auto uses bfloat16, the precision the "
            "published checkpoint is stored in; float32 doubles the host memory the load needs."
        ),
    )
    parser.add_argument("--language", default="en")
    parser.add_argument("--prompt", default="Transcribe the following:")
    return parser.parse_args(argv)


def nemo_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("nemo") is not None


def download_model(model: str) -> int:
    """Populate the HuggingFace cache with the checkpoint, then exit.

    The backend calls this at startup so the first assessment does not stall
    for a ~5 GB download. It deliberately does not construct the model: caching
    the files is all that is needed, and instantiating a 2.5B-parameter SALM
    would claim GPU memory the API process has no use for. NeMo's own
    ``from_pretrained`` resolves the same cache, so the later run finds the
    weights already on disk.
    """
    from huggingface_hub import snapshot_download

    path = snapshot_download(
        repo_id=model,
        token=os.getenv("HF_TOKEN") or os.getenv("WHISPERX_HF_TOKEN") or None,
    )
    # The engine's prefetch greps for this exact token.
    print(f"model-ready {path}", flush=True)
    return 0


def resolve_device(requested: str) -> str:
    if requested != "auto":
        return requested
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def plan_windows(duration: float, chunk_seconds: float, overlap_seconds: float) -> list[tuple[float, float]]:
    """Split a duration into overlapping windows.

    The overlap exists so a word spoken across a boundary is heard whole by at
    least one window; it is bounded below the chunk length because a stride of
    zero or less would never advance.
    """
    chunk = max(float(chunk_seconds), 1.0)
    overlap = min(max(float(overlap_seconds), 0.0), chunk - 1.0)
    stride = chunk - overlap
    windows: list[tuple[float, float]] = []
    start = 0.0
    while start < duration:
        end = min(start + chunk, duration)
        windows.append((start, end))
        if end >= duration:
            break
        start += stride
    return windows or [(0.0, float(duration))]


def load_audio(path: Path) -> tuple[Any, int]:
    import soundfile

    samples, sample_rate = soundfile.read(str(path), dtype="float32", always_2d=False)
    if getattr(samples, "ndim", 1) > 1:
        samples = samples.mean(axis=1)
    return samples, int(sample_rate)


def supports_map_location(loader: Any) -> bool:
    """Whether ``from_pretrained`` takes ``map_location`` as a real parameter.

    Checked rather than assumed: huggingface_hub's mixin declares it, but an
    older or patched NeMo would swallow the keyword into ``**model_kwargs`` and
    forward it to the model constructor, where it fails as something that reads
    nothing like a loading problem.
    """
    import inspect

    try:
        parameters = inspect.signature(loader).parameters
    except (TypeError, ValueError):
        return False
    return "map_location" in parameters


def available_commit_bytes() -> int | None:
    """Memory this process could still commit, or ``None`` if it cannot be read.

    On Windows the number that matters is not free RAM but the commit charge
    the system will still accept — ``ullAvailPageFile`` — because a refused
    charge is what the allocator turns into an access violation. Elsewhere the
    kernel's own estimate of allocatable memory is the equivalent.
    """
    if os.name == "nt":
        import ctypes

        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(MemoryStatusEx)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return int(status.ullAvailPageFile)
    try:
        import psutil

        return int(psutil.virtual_memory().available)
    except Exception:  # psutil is optional everywhere but the preflight
        return None


def checkpoint_bytes(model: str) -> int | None:
    """Size of the cached checkpoint, or ``None`` if it is not on disk yet."""
    try:
        from huggingface_hub import try_to_load_from_cache
    except Exception:
        return None
    path = try_to_load_from_cache(model, "model.safetensors")
    if not isinstance(path, str):
        return None
    try:
        return Path(path).stat().st_size
    except OSError:
        return None


def preflight_memory(model: str, headroom_ratio: float = HEADROOM_RATIO) -> str | None:
    """Say why this host cannot load ``model``, before spending a minute proving it.

    The load claims roughly the checkpoint's size again for the module tree,
    plus room for activations. When the commit the system will grant is below
    that, the run does not fail — it *crashes*, because a refused commit reaches
    torch as a null buffer it writes to anyway. Refusing up front turns a
    silent access violation into a sentence the operator can act on, and hands
    the session to the fallback engine forty minutes earlier.

    Returns ``None`` when there is nothing to object to, including when either
    number cannot be read: a preflight that guesses would block good runs.
    """
    available = available_commit_bytes()
    checkpoint = checkpoint_bytes(model)
    if available is None or checkpoint is None:
        return None
    required = int(checkpoint * headroom_ratio)
    if available >= required:
        return None
    return (
        f"{model} needs about {required / 2**30:.1f} GB of committable memory to load "
        f"(a {checkpoint / 2**30:.1f} GB checkpoint plus the model it fills), and this host "
        f"will currently grant {available / 2**30:.1f} GB."
    )


def resolve_load_dtype(requested: str) -> Any:
    """Torch dtype the parameters are built in. ``auto`` means the checkpoint's."""
    import torch

    name = DEFAULT_LOAD_DTYPE if requested in (None, "", "auto") else str(requested)
    dtype = getattr(torch, name, None)
    if dtype is None:
        raise ValueError(f"Unknown dtype {requested!r}; choose one of {', '.join(LOAD_DTYPE_CHOICES)}.")
    return dtype


@contextlib.contextmanager
def built_in_dtype(dtype: Any):
    """Build SALM's submodules in ``dtype`` instead of float32.

    Two separate float32 defaults have to be displaced, because SALM assembles
    itself from two halves:

    * the Conformer encoder is instantiated through Hydra with no dtype
      anywhere, so it follows torch's process-wide default;
    * the Qwen backbone is built by ``load_pretrained_hf``, whose own signature
      defaults to ``torch.float32`` and therefore ignores that default. NeMo
      offers no config hook for it — ``cfg['torch_dtype']`` is only consulted on
      the Automodel path, which this checkpoint does not take — so the function
      SALM imported is wrapped for the duration of the load.

    Both are restored on the way out, including when the load raises, so a
    retry and anything else in this interpreter see the normal defaults.
    """
    import torch

    previous_default = torch.get_default_dtype()
    torch.set_default_dtype(dtype)
    try:
        import nemo.collections.speechlm2.models.salm as salm_module
    except Exception:  # a NeMo layout without this module still gets the torch default
        salm_module = None
    original_loader = getattr(salm_module, "load_pretrained_hf", None) if salm_module else None
    if original_loader is not None:

        def load_pretrained_hf(*args: Any, **kwargs: Any) -> Any:
            kwargs.setdefault("dtype", dtype)
            return original_loader(*args, **kwargs)

        salm_module.load_pretrained_hf = load_pretrained_hf
    try:
        yield
    finally:
        if original_loader is not None:
            salm_module.load_pretrained_hf = original_loader
        torch.set_default_dtype(previous_default)


def copy_checkpoint_into(model: Any, checkpoint_path: str, strict: bool = False) -> tuple[int, list[str]]:
    """Copy a safetensors checkpoint into an already-built model, one tensor at a time.

    ``huggingface_hub`` materialises the whole file into a state dict and then
    calls ``load_state_dict``, so the host briefly holds the model *and* a
    second complete copy of its weights — 4.6 GB plus 5.1 GB for this
    checkpoint. That peak is what the machine could not commit, and it is
    entirely avoidable: the parameters already exist and only need their values,
    so the file is streamed and each tensor is copied and dropped.

    Non-strict by default, matching NeMo, which builds the backbone unweighted
    and expects the checkpoint to fill it. Names the file does not carry are
    returned so the caller can say so; tied parameters share storage and are
    filled by their partner's copy.
    """
    import torch
    from safetensors import safe_open

    targets = dict(model.named_parameters())
    targets.update(dict(model.named_buffers()))
    copied = 0
    missing: list[str] = []
    with safe_open(checkpoint_path, framework="pt", device="cpu") as handle:
        available = set(handle.keys())
        for name, target in targets.items():
            if name not in available:
                missing.append(name)
                continue
            with torch.no_grad():
                target.copy_(handle.get_tensor(name))
            copied += 1
        unexpected = sorted(available - set(targets))
    if strict and (missing or unexpected):
        raise RuntimeError(f"Checkpoint does not match the model (missing={missing}, unexpected={unexpected}).")
    return copied, missing


@contextlib.contextmanager
def streamed_checkpoint_load():
    """Make ``from_pretrained`` fill the model in place instead of via a state dict.

    Patches the one hub method that stages the weights, for the duration of the
    load only. If the installed ``huggingface_hub`` does not have it, the load
    proceeds unpatched — slower to fail on a small host, but never wrong.
    """
    try:
        from huggingface_hub.hub_mixin import PyTorchModelHubMixin
    except Exception:  # a hub layout this shim does not know
        yield
        return
    # Taken from the class dict, not by attribute access: the latter hands back
    # a freshly bound method, and restoring that would leave a bound method
    # where a classmethod object used to be.
    missing = object()
    original = PyTorchModelHubMixin.__dict__.get("_load_as_safetensor", missing)
    if original is missing:
        yield
        return

    @classmethod  # type: ignore[misc]
    def _load_as_safetensor(_cls: Any, model: Any, model_file: str, _map_location: str, strict: bool) -> Any:
        copied, missing = copy_checkpoint_into(model, model_file, strict=strict)
        print(f"Applied {copied} checkpoint tensor(s); {len(missing)} not in the file.", flush=True)
        model.eval()
        return model

    PyTorchModelHubMixin._load_as_safetensor = _load_as_safetensor
    try:
        yield
    finally:
        PyTorchModelHubMixin._load_as_safetensor = original


# The one module that is not built in the load dtype: it runs an STFT over the
# raw float32 waveform and hands float32 features to the encoder. Every module
# whose name ends in this, and everything beneath it, is kept in float32.
FRONTEND_MODULE_NAME = "preprocessor"


@dataclass(frozen=True)
class DtypeAlignment:
    """What :func:`align_model_dtype` changed, for the log line the run prints."""

    dtype: Any
    frontends: tuple[str, ...]
    converted: tuple[str, ...]

    def describe(self, sample: int = 3) -> str:
        shown = ", ".join(self.converted[:sample])
        if len(self.converted) > sample:
            shown += f", ... ({len(self.converted)} in total)"
        converted = f"converted {len(self.converted)} parameter(s) built in float32 ({shown})" if shown else "nothing to convert"
        frontends = ", ".join(self.frontends) or "none"
        return f"Aligned the model to {self.dtype}: front-end kept in float32 ({frontends}); {converted}."


def _is_frontend(name: str) -> bool:
    return name.rsplit(".", 1)[-1] == FRONTEND_MODULE_NAME


def _under_frontend(name: str, frontends: tuple[str, ...]) -> bool:
    return any(name == frontend or name.startswith(f"{frontend}.") for frontend in frontends)


def align_model_dtype(model: Any, dtype: Any) -> DtypeAlignment:
    """Make every floating parameter agree on ``dtype`` — except the mel front-end.

    Building the model under a ``dtype`` default only converts what honours
    that default, and NeMo does not everywhere: each Conformer attention layer
    creates its relative-position biases with ``torch.FloatTensor(h, d_k)``,
    which is float32 whatever the default says, and PEFT upcasts the LoRA
    adapters to float32 on purpose. ``copy_`` then keeps the destination's
    dtype, so the checkpoint's bfloat16 values are widened into those float32
    parameters and the mismatch survives the load intact. It surfaced only in
    the first forward — ``expected scalar type Float but found BFloat16`` where
    ``q + pos_bias_v`` (promoted to float32) met ``linear_pos(pos_emb)``
    (bfloat16) — after the whole checkpoint had been streamed in.

    So the load is followed by one invariant rather than a patch per module:
    every floating-point *parameter* outside the front-end is in ``dtype``.
    That costs nothing in precision — the checkpoint holds nothing wider than
    bfloat16 — and the converted names are returned so the next hard-coded
    float32 a NeMo upgrade introduces shows up in the run's log rather than as
    a traceback under ``generate``.

    Buffers are left as their owners made them: the Qwen rotary ``inv_freq`` is
    float32 by design (transformers computes positions in float32 and would
    not recover the precision from a narrowed copy), and NeMo's positional
    table is already created in the encoder's dtype.

    The front-end is the exception in the other direction. It cannot follow the
    rest into bfloat16 — an STFT over the raw float32 waveform returns float32
    features no matter what dtype its own buffers are in — so it stays float32
    and its output is cast once on the way into the encoder. Lengths and other
    integer outputs pass through untouched. Without that cast the encoder's
    first convolution rejects the features (``Input type (float) and bias type
    (struct c10::BFloat16) should be the same``).
    """
    frontends: list[str] = []
    converted: list[str] = []

    def cast(value: Any) -> Any:
        if hasattr(value, "is_floating_point") and value.is_floating_point():
            return value.to(dtype)
        return value

    for name, module in model.named_modules():
        if not _is_frontend(name):
            continue
        module.float()
        original_forward = module.forward

        def forward(*args: Any, _original_forward: Any = original_forward, **kwargs: Any) -> Any:
            outputs = _original_forward(*args, **kwargs)
            if isinstance(outputs, tuple):
                return tuple(cast(item) for item in outputs)
            return cast(outputs)

        module.forward = forward
        frontends.append(name)

    for name, module in model.named_modules():
        if _under_frontend(name, tuple(frontends)):
            continue
        for parameter_name, parameter in module.named_parameters(recurse=False):
            if not parameter.is_floating_point() or parameter.dtype == dtype:
                continue
            parameter.data = parameter.data.to(dtype)
            converted.append(f"{name}.{parameter_name}" if name else parameter_name)

    return DtypeAlignment(dtype=dtype, frontends=tuple(frontends), converted=tuple(converted))


def load_salm(salm_class: Any, model_id: str, device: str, dtype: Any = None) -> tuple[Any, str]:
    """Load the checkpoint for ``device``, degrading rather than dying.

    Three things here are the difference between a working run and OS error
    1455 — or an access violation — on a machine with ordinary commit headroom:

    * the parameters are built in the checkpoint's own bfloat16 rather than
      float32, halving the ~9 GB the host had to commit to hold a model whose
      values were never wider than bfloat16;
    * the checkpoint is streamed into the parameters that already exist rather
      than materialised as a second complete copy of the model first, which is
      what ``huggingface_hub`` does and what took the peak from ~4.6 GB to
      ~9.7 GB — past what this host could commit;
    * the weights are staged in **host** memory even for a CUDA run. Reading
      them straight onto the GPU sounds like the way to avoid that commit, but
      the module tree is constructed on the host regardless, and asking
      safetensors for CUDA tensors additionally fails outright on this stack —
      ``RuntimeError: Attempted to access the data pointer on an invalid python
      storage`` — so every CUDA run silently paid for a load twice. The move to
      the device happens afterwards, once;
    * a load that still exhausts memory retries on CPU, and the failed attempt
      is released **first**. Holding the exception held its traceback, which
      held the frame that built the half-finished model, so the retry began
      with the previous attempt's several gigabytes still charged and could
      only fail the same way. Only its text is kept.

    Returns the model and the device it actually loaded on. Raises
    :class:`InsufficientMemory` only when no device could hold it.
    """
    attempts = [device] if device == "cpu" else [device, "cpu"]
    last_error_text = ""
    for attempt_device in attempts:
        kwargs = {"map_location": "cpu"} if supports_map_location(salm_class.from_pretrained) else {}
        failure: str | None = None
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(streamed_checkpoint_load())
                if dtype is not None:
                    stack.enter_context(built_in_dtype(dtype))
                model = salm_class.from_pretrained(model_id, **kwargs)
        except BaseException as error:  # noqa: BLE001 - re-raised here unless it is exhaustion
            if not is_memory_exhaustion(error):
                raise
            failure = f"{type(error).__name__}: {error}"
        if failure is None:
            return model, attempt_device
        # Outside the handler on purpose: the exception, its traceback and the
        # partially built model it pins are unreachable by now, so the collect
        # below can actually give the memory back before the next attempt.
        last_error_text = failure
        print(
            f"Loading {model_id} for {attempt_device} ran out of memory ({failure}).",
            file=sys.stderr,
            flush=True,
        )
        free_memory()
    raise InsufficientMemory(last_error_text or "unknown allocation failure")


def free_memory() -> None:
    """Return whatever the failed attempt claimed before trying a smaller one."""
    import gc

    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # torch missing or a driver that dislikes being asked
        pass


def transcribe_batch(
    model: Any, audio_paths: list[Path], prompt: str, generation_config: Any = None
) -> list[str]:
    """One SALM generation call for a batch of windows, in window order.

    Purely a throughput grouping: each window is still its own independent
    forward pass (no cross-window state), so batching windows together changes
    how many share one generate() call and nothing about any one window's
    output. ``ids_to_text`` strips padding/eos tokens per row by default, so a
    shorter answer sharing a batch with a longer one decodes clean.
    """
    answer_ids = model.generate(
        prompts=[
            [{"role": "user", "content": f"{prompt} {model.audio_locator_tag}", "audio": [str(path)]}]
            for path in audio_paths
        ],
        max_new_tokens=MAX_NEW_TOKENS_PER_WINDOW,
        generation_config=generation_config,
    )
    return [str(model.tokenizer.ids_to_text(row.cpu())).strip() for row in answer_ids]


def run(args: argparse.Namespace) -> int:
    import tempfile

    import soundfile
    from transformers import GenerationConfig

    from nemo.collections.speechlm2.models import SALM

    samples, sample_rate = load_audio(args.audio)
    duration = len(samples) / float(sample_rate or TARGET_SAMPLE_RATE)
    windows = plan_windows(duration, args.chunk_seconds, args.overlap_seconds)

    shortage = preflight_memory(args.model)
    if shortage is not None:
        raise InsufficientMemory(shortage)

    device = resolve_device(args.device)
    dtype = resolve_load_dtype(getattr(args, "dtype", "auto"))
    print(
        f"Loading {args.model} on {device} in {dtype} for {duration:.1f}s of audio "
        f"in {len(windows)} window(s).",
        flush=True,
    )
    model, device = load_salm(SALM, args.model, device, dtype=dtype)
    print(align_model_dtype(model, dtype).describe(), flush=True)
    if hasattr(model, "to"):
        model = model.to(device)
    if hasattr(model, "eval"):
        model.eval()

    generation_config = GenerationConfig(
        bos_token_id=model.text_bos_id,
        eos_token_id=model.text_eos_id,
        pad_token_id=model.text_pad_id,
        repetition_penalty=DEFAULT_REPETITION_PENALTY,
        no_repeat_ngram_size=DEFAULT_NO_REPEAT_NGRAM_SIZE,
    )

    batch_size = max(int(getattr(args, "batch_size", 1) or 1), 1)
    segments: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="canary-") as temp_dir:
        for batch_start in range(0, len(windows), batch_size):
            batch = windows[batch_start : batch_start + batch_size]
            window_paths = []
            for offset, (start, end) in enumerate(batch):
                window_path = Path(temp_dir) / f"window-{batch_start + offset:04d}.wav"
                first = int(start * sample_rate)
                last = int(end * sample_rate)
                soundfile.write(str(window_path), samples[first:last], sample_rate)
                window_paths.append(window_path)
            texts = transcribe_batch(model, window_paths, args.prompt, generation_config)
            for (start, end), text in zip(batch, texts):
                if text:
                    segments.append({"start": round(start, 3), "end": round(end, 3), "text": text})
            # Parsed by the engine's output handler into live step progress.
            done = min(batch_start + batch_size, len(windows))
            print(f"Progress: {(done / len(windows)) * 100:.2f}%...", flush=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "model": args.model,
                "language": args.language,
                "device": device,
                "dtype": str(dtype).replace("torch.", ""),
                "durationSeconds": round(duration, 3),
                "segments": segments,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {len(segments)} segment(s) to {args.output}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.check:
        # The engine's availability probe greps for this exact token.
        print("nemo-ready" if nemo_available() else "nemo-missing", flush=True)
        return 0
    if args.download:
        if not nemo_available():
            print("nemo-missing", flush=True)
            return 3
        try:
            return download_model(args.model)
        except Exception as error:  # offline, gated repo, bad id
            # Typed, because hub errors are sometimes raised with no message
            # and "download failed:" on its own diagnoses nothing.
            print(f"model-download-failed: {type(error).__name__}: {error}", file=sys.stderr)
            return 4
    if args.audio is None or args.output is None:
        print("--audio and --output are required.", file=sys.stderr)
        return 2
    if not nemo_available():
        print(
            "The NeMo toolkit is not installed. Install it with: uv sync --group canary",
            file=sys.stderr,
        )
        return 3
    try:
        return run(args)
    except InsufficientMemory as error:
        # One line, one token, no traceback: the engine turns this into a typed
        # failure the queue will not waste attempts re-running.
        print(f"{INSUFFICIENT_MEMORY_TOKEN} {error}", file=sys.stderr, flush=True)
        return EXIT_INSUFFICIENT_MEMORY
    except BaseException as error:  # noqa: BLE001 - classified, then re-raised
        if not is_memory_exhaustion(error):
            raise
        # Exhaustion outside the load — a decode window, or an allocation NeMo
        # makes on its own — reads the same to the operator and is just as
        # permanent on this host.
        print(f"{INSUFFICIENT_MEMORY_TOKEN} {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        return EXIT_INSUFFICIENT_MEMORY


if __name__ == "__main__":
    raise SystemExit(main())
