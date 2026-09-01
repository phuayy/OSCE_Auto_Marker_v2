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
    model = SALM.from_pretrained(args.model)
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
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
