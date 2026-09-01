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
import json
import os
import sys
from pathlib import Path
from typing import Any

SCHEMA = "canary-segments-v1"
TARGET_SAMPLE_RATE = 16000

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


def is_memory_exhaustion(error: BaseException) -> bool:
    """Whether ``error`` is the host running out of memory rather than a bug.

    Matched by signature, not by type: safetensors surfaces the Windows commit
    limit as a bare ``OSError``, torch raises ``RuntimeError`` for both CUDA and
    CPU allocator failures, and the C++ allocator raises ``MemoryError``. What
    they share is the wording, so that is what is checked — plus the two error
    numbers that carry no wording at all (WinError 1455, ENOMEM).
    """
    if isinstance(error, MemoryError):
        return True
    if isinstance(error, OSError):
        if getattr(error, "winerror", None) == 1455 or error.errno == 12:
            return True
    text = f"{type(error).__name__}: {error}".lower()
    return any(signature in text for signature in _MEMORY_ERROR_SIGNATURES)


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


def load_salm(salm_class: Any, model_id: str, device: str) -> tuple[Any, str]:
    """Load the checkpoint for ``device``, degrading rather than dying.

    Two things here are the difference between a working run and OS error 1455
    on a machine with a small pagefile:

    * the weights are read **straight onto the target device**. The default
      loads all ~5 GB into host memory first and only then copies to the GPU,
      so a CUDA run needed the host commit it was trying to avoid;
    * a CUDA load that still exhausts memory retries on CPU. Slow beats failed:
      the alternative is a dead session, and the operator can switch engines
      afterwards knowing the transcript exists.

    Returns the model and the device it actually loaded on. Raises
    :class:`InsufficientMemory` only when no device could hold it.
    """
    attempts = [device] if device == "cpu" else [device, "cpu"]
    last_error: BaseException | None = None
    for attempt_device in attempts:
        kwargs = {"map_location": attempt_device} if supports_map_location(salm_class.from_pretrained) else {}
        try:
            model = salm_class.from_pretrained(model_id, **kwargs)
        except BaseException as error:  # noqa: BLE001 - re-raised below unless it is exhaustion
            if not is_memory_exhaustion(error):
                raise
            last_error = error
            print(
                f"Loading {model_id} on {attempt_device} ran out of memory ({type(error).__name__}: {error}).",
                file=sys.stderr,
                flush=True,
            )
            free_memory()
            continue
        return model, attempt_device
    raise InsufficientMemory(str(last_error) if last_error is not None else "unknown allocation failure")


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


def transcribe_window(model: Any, audio_path: Path, prompt: str) -> str:
    """One SALM generation call for one window of audio."""
    answer_ids = model.generate(
        prompts=[[{"role": "user", "content": f"{prompt} {model.audio_locator_tag}", "audio": [str(audio_path)]}]],
        max_new_tokens=512,
    )
    return str(model.tokenizer.ids_to_text(answer_ids[0].cpu())).strip()


def run(args: argparse.Namespace) -> int:
    import tempfile

    import soundfile

    from nemo.collections.speechlm2.models import SALM

    samples, sample_rate = load_audio(args.audio)
    duration = len(samples) / float(sample_rate or TARGET_SAMPLE_RATE)
    windows = plan_windows(duration, args.chunk_seconds, args.overlap_seconds)

    device = resolve_device(args.device)
    print(f"Loading {args.model} on {device} for {duration:.1f}s of audio in {len(windows)} window(s).", flush=True)
    model, device = load_salm(SALM, args.model, device)
    if hasattr(model, "to"):
        model = model.to(device)
    if hasattr(model, "eval"):
        model.eval()

    segments: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="canary-") as temp_dir:
        for index, (start, end) in enumerate(windows):
            window_path = Path(temp_dir) / f"window-{index:04d}.wav"
            first = int(start * sample_rate)
            last = int(end * sample_rate)
            soundfile.write(str(window_path), samples[first:last], sample_rate)
            text = transcribe_window(model, window_path, args.prompt)
            if text:
                segments.append({"start": round(start, 3), "end": round(end, 3), "text": text})
            # Parsed by the engine's output handler into live step progress.
            print(f"Progress: {((index + 1) / len(windows)) * 100:.2f}%...", flush=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "model": args.model,
                "language": args.language,
                "device": device,
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
            'The NeMo toolkit is not installed. Install it with: pip install "nemo_toolkit[asr]>=2.5"',
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
