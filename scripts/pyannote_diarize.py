"""Standalone speaker diarisation with pyannote.audio.

WhisperX diarises as part of its own run; every other engine returns plain
text, so this script supplies the speaker turns those transcripts are missing.
It writes the turn list the pipeline merges onto the segments — start, end and
speaker label, in seconds.

Runs as a subprocess so the pyannote/torch stack stays out of the API process,
and so a diarisation crash cannot take the API down with it.

Usage:
    python scripts/pyannote_diarize.py --audio in.wav --output turns.json \
        --min-speakers 2 --max-speakers 2
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

SCHEMA = "speaker-turns-v1"
DEFAULT_MODEL = "pyannote/speaker-diarization-community-1"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diarize an audio file with pyannote.audio.")
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    parser.add_argument("--min-speakers", type=int, default=0)
    parser.add_argument("--max-speakers", type=int, default=0)
    return parser.parse_args(argv)


def resolve_device(requested: str) -> str:
    if requested != "auto":
        return requested
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def speaker_bounds(args: argparse.Namespace) -> dict[str, int]:
    """Only pass bounds that were actually configured; 0 means "unknown", and
    pyannote estimates the count when neither bound is given."""
    bounds: dict[str, int] = {}
    minimum = max(int(args.min_speakers), 0)
    maximum = max(int(args.max_speakers), 0)
    if minimum and maximum:
        minimum = min(minimum, maximum)
    if minimum:
        bounds["min_speakers"] = minimum
    if maximum:
        bounds["max_speakers"] = maximum
    return bounds


def run(args: argparse.Namespace) -> int:
    import torch
    from pyannote.audio import Pipeline

    token = os.getenv("HF_TOKEN") or os.getenv("WHISPERX_HF_TOKEN") or None
    print(f"Loading {args.model}...", flush=True)
    pipeline = Pipeline.from_pretrained(args.model, use_auth_token=token)
    if pipeline is None:
        print(
            f"Could not load '{args.model}'. The model is gated on HuggingFace: accept its "
            "conditions and provide a token via HF_TOKEN.",
            file=sys.stderr,
        )
        return 4

    device = resolve_device(args.device)
    pipeline.to(torch.device(device))
    print(f"Diarizing {args.audio.name} on {device}...", flush=True)
    annotation = pipeline(str(args.audio), **speaker_bounds(args))

    turns: list[dict[str, Any]] = [
        {"start": round(float(segment.start), 3), "end": round(float(segment.end), 3), "speaker": str(speaker)}
        for segment, _track, speaker in annotation.itertracks(yield_label=True)
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"schema": SCHEMA, "model": args.model, "device": device, "turns": turns}, indent=2),
        encoding="utf-8",
    )
    speakers = sorted({turn["speaker"] for turn in turns})
    print(f"Wrote {len(turns)} turn(s) for {len(speakers)} speaker(s) to {args.output}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except ImportError:
        print(
            "pyannote.audio is not installed. It ships with the backend requirements; "
            "install them into the interpreter running the scorers.",
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
