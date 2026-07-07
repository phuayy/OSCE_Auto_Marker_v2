#!/usr/bin/env python3
"""Detect OSCE session boundaries from *person presence* using RT-DETR.

Experimental vision-based alternative to ``detect_bell_segments.py``. Instead
of listening for transition bells, this script samples video frames at a low
rate (default 1 frame/second), counts people per frame with an RT-DETR object
detector, and derives student clip ranges from sustained changes in person
count:

* A session is ACTIVE while at least ``--min-people`` (default 2: the student
  plus the patient/examiner) are visible.
* A session ENDS when fewer than ``--min-people`` are visible continuously for
  ``--end-after-seconds`` (default 8.0 s ~= 240 frames of a 30 fps source, per
  the project requirement). The clip is closed at the moment the low-person
  run *began* — that is when someone actually left the room — not when the
  threshold was crossed.
* The next session STARTS once >= ``--min-people`` are visible continuously
  for ``--start-after-seconds`` (a debounce so a person briefly crossing the
  frame does not open a phantom session).

Robustness against detector flicker (the model missing a person for a few
frames) is layered — no single mis-detection can flip a boundary:

1. **Sparse sampling** (default 1 fps) means a "few bad frames" at native fps
   usually never reach the classifier at all.
2. A **median filter** (``--median-window``) over the per-sample person counts
   removes single-sample spikes/dips.
3. **Morphological closing** on the boolean in-session signal fills
   contradictory blips shorter than ``--flicker-tolerance-seconds`` in BOTH
   directions (a missed person during a session, a phantom person during a
   gap).
4. The **hysteresis state machine** itself only reacts to runs longer than the
   start/end confirmation windows; anything shorter is absorbed into the
   current state.

Model selection (researched for an RTX 3050 Laptop GPU, 4 GB VRAM, sharing the
machine with WhisperX ``large-v2``):

===========================================  ========  =================  ==========================
Model (HuggingFace id)                       Params    VRAM @640 (fp16)   Notes
===========================================  ========  =================  ==========================
PekingU/rtdetr_v2_r18vd  (DEFAULT)           ~20 M     ~1.2-1.6 GB        RT-DETRv2, best fit; ~46 AP
PekingU/rtdetr_r18vd                         ~20 M     ~1.2-1.6 GB        v1 fallback, ~46 AP
PekingU/rtdetr_r50vd                         ~42 M     ~2.4-2.8 GB        tight on 4 GB, marginal gain
PekingU/rtdetr_r101vd                        ~76 M     > 4 GB             do not use on this GPU
===========================================  ========  =================  ==========================

Person counting is a COCO-easy task; the R18 backbone is more than sufficient
and leaves headroom. All variants load through ``transformers``
(``AutoModelForObjectDetection``), which is already installed for WhisperX —
this script adds **no new dependencies** (frames are decoded via the ffmpeg
binary the pipeline already requires; no OpenCV).

Sequential execution with WhisperX is guaranteed architecturally: this script
runs as a short-lived subprocess inside the ``auto_crop`` job, and WhisperX
only runs later inside per-clip ``process_session`` jobs — segmentation always
completes (and this process exits, releasing ALL of its VRAM) before any
transcription starts for those clips. Set ``JOB_WORKER_CONCURRENCY=1`` if you
also need cross-session exclusivity on the GPU.

CLI contract mirrors ``detect_bell_segments.py``: progress goes to stderr,
stdout carries exactly one JSON document whose ``clip_ranges`` key holds
``[{"start": float, "end": float, "student_index": int}, ...]`` — the same
shape ``MediaPipeline.normalize_clip_ranges`` / ``build_clip_drafts_from_ranges``
already consume, so wiring it into ``media.py`` later is a drop-in.

Usage:
    python scripts/detect_human_segments.py \
        --video storage/input/videos/<session>.mp4 --video-duration 3600 \
        [--sample-fps 1.0] [--end-after-seconds 8] [--output out.json]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# Environment helpers.
#
# Deliberately duplicated per script (same as the scoring scripts): each script
# runs as an isolated subprocess and must stay importable without the FastAPI
# app package on sys.path.
# ---------------------------------------------------------------------------


def read_float_env(name: str, default: float) -> float:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def read_int_env(name: str, default: int) -> int:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def log(message: str) -> None:
    """Progress/diagnostic line. stderr only — stdout is reserved for the JSON
    contract (CommandRunner streams stderr into the live SSE log)."""
    print(f"[human-segments] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "PekingU/rtdetr_v2_r18vd"
# Ordered fallbacks when the default model id cannot be loaded (e.g. an older
# transformers without RT-DETRv2 support). All are person-capable COCO models.
FALLBACK_MODELS = ("PekingU/rtdetr_r18vd", "PekingU/rtdetr_r50vd")

DETECTOR_NAME = "osce-human-presence-rtdetr-v1"


@dataclass(frozen=True)
class SegmenterConfig:
    """Pure segmentation parameters (no I/O concerns) — unit-testable."""

    sample_fps: float = 1.0
    min_people: int = 2
    start_after_seconds: float = 4.0
    end_after_seconds: float = 8.0
    flicker_tolerance_seconds: float = 2.0
    median_window: int = 3
    start_offset_seconds: float = 0.0
    end_offset_seconds: float = 2.0
    min_clip_seconds: float = 0.5

    @property
    def sample_dt(self) -> float:
        return 1.0 / max(0.01, self.sample_fps)

    def seconds_to_samples(self, seconds: float) -> int:
        return max(1, round(seconds / self.sample_dt))


@dataclass(frozen=True)
class DetectorConfig:
    model_id: str = DEFAULT_MODEL
    device: str = "auto"  # auto | cuda | cpu
    confidence: float = 0.5
    batch_size: int = 8
    decode_width: int = 640  # ffmpeg pre-scale; the processor re-sizes anyway


@dataclass
class DetectionStats:
    """Runtime diagnostics surfaced in the output payload for tuning."""

    decode_seconds: float = 0.0
    inference_seconds: float = 0.0
    sampled_frames: int = 0
    oom_batch_reductions: int = 0
    cpu_fallback: bool = False
    effective_batch_size: int = 0
    person_count_histogram: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Frame decoding (ffmpeg pipe — no OpenCV dependency)
# ---------------------------------------------------------------------------


def resolve_binary(env_name: str, default: str) -> str:
    candidate = str(os.getenv(env_name) or "").strip() or default
    resolved = shutil.which(candidate)
    if resolved:
        return resolved
    if Path(candidate).exists():
        return candidate
    raise RuntimeError(
        f"{default} was not found in PATH (checked {candidate!r}). "
        f"Install ffmpeg or set {env_name}."
    )


def probe_video_dimensions(ffprobe_bin: str, video_path: Path) -> tuple[int, int]:
    result = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe could not read the video: {result.stderr.strip()[:400]}")
    try:
        stream = json.loads(result.stdout)["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
    except (KeyError, IndexError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("ffprobe returned no readable video stream metadata.") from error
    if width <= 0 or height <= 0:
        raise RuntimeError(f"Video reports invalid dimensions {width}x{height}.")
    return width, height


def iter_sampled_frames(
    ffmpeg_bin: str,
    video_path: Path,
    *,
    sample_fps: float,
    out_width: int,
    out_height: int,
) -> Iterator["Any"]:
    """Yield RGB numpy frames sampled at ``sample_fps`` via an ffmpeg rawvideo pipe.

    Sampling in ffmpeg (``fps=`` filter) means the Python side only ever sees
    ~1 frame per second — the detector never touches the other 29+ frames, which
    is what keeps this workflow light enough to share a laptop GPU.
    """
    import numpy as np

    frame_bytes = out_width * out_height * 3
    args = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-vf",
        f"fps={sample_fps},scale={out_width}:{out_height}",
        "-pix_fmt",
        "rgb24",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None
    try:
        while True:
            chunk = process.stdout.read(frame_bytes)
            if not chunk or len(chunk) < frame_bytes:
                break
            yield np.frombuffer(chunk, dtype=np.uint8).reshape((out_height, out_width, 3))
    finally:
        process.stdout.close()
        stderr_tail = b""
        if process.stderr is not None:
            stderr_tail = process.stderr.read() or b""
            process.stderr.close()
        return_code = process.wait()
        if return_code not in (0, None):
            raise RuntimeError(
                f"ffmpeg frame decoding failed (exit {return_code}): "
                f"{stderr_tail.decode('utf-8', 'replace').strip()[:400]}"
            )


# ---------------------------------------------------------------------------
# Person detection (RT-DETR via HuggingFace transformers)
# ---------------------------------------------------------------------------


class PersonCounter:
    """Batched people-counter around an RT-DETR checkpoint.

    Owns the model lifecycle so VRAM is held for the shortest possible span:
    ``load()`` -> ``count_people()`` batches -> ``close()`` (and process exit
    frees everything regardless).
    """

    def __init__(self, config: DetectorConfig) -> None:
        self.config = config
        self.model = None
        self.processor = None
        self.device = "cpu"
        self.use_fp16 = False
        self.person_label_ids: set[int] = set()
        self.resolved_model_id = config.model_id

    def load(self) -> None:
        try:
            import torch
            from transformers import AutoImageProcessor, AutoModelForObjectDetection
        except ImportError as error:
            raise RuntimeError(
                "Missing detector dependencies. Install with: "
                "pip install torch transformers pillow numpy"
            ) from error

        if self.config.device == "auto":
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            self.device = self.config.device
        if self.device == "cuda" and not torch.cuda.is_available():
            log("CUDA requested but unavailable — falling back to CPU (slower).")
            self.device = "cpu"
        # fp16 halves VRAM on GPU; CPU inference stays fp32 (no CPU fp16 kernels).
        self.use_fp16 = self.device == "cuda"

        candidates = [self.config.model_id]
        candidates.extend(m for m in FALLBACK_MODELS if m not in candidates)
        last_error: Exception | None = None
        for model_id in candidates:
            try:
                log(f"Loading detector {model_id} on {self.device} (fp16={self.use_fp16})...")
                self.processor = AutoImageProcessor.from_pretrained(model_id)
                model = AutoModelForObjectDetection.from_pretrained(model_id)
                self.resolved_model_id = model_id
                break
            except Exception as error:  # noqa: BLE001 — surface the last cause below
                last_error = error
                log(f"Could not load {model_id}: {error}")
                model = None
        if model is None:
            raise RuntimeError(
                "No RT-DETR checkpoint could be loaded. First run needs internet "
                "access to download weights (~80 MB) into the HuggingFace cache "
                f"(HF_HOME={os.getenv('HF_HOME') or '~/.cache/huggingface'}). "
                f"Last error: {last_error}"
            )

        model = model.to(self.device)
        if self.use_fp16:
            model = model.half()
        model.eval()
        self.model = model

        # Resolve the "person" class id from the checkpoint's label map instead
        # of hardcoding COCO index 0 — keeps alternate checkpoints working.
        id2label = getattr(model.config, "id2label", {}) or {}
        self.person_label_ids = {
            int(idx) for idx, label in id2label.items() if str(label).strip().lower() == "person"
        }
        if not self.person_label_ids:
            log("Label map has no explicit 'person' class; assuming COCO id 0.")
            self.person_label_ids = {0}

    def count_people(self, frames: list["Any"], stats: DetectionStats) -> list[int]:
        """Person count per frame for one batch, with OOM-degradation.

        On CUDA OOM the batch is halved (recursively) and, if a single frame
        still does not fit, the whole model is moved to CPU — the run degrades
        instead of failing.
        """
        import torch

        if not frames:
            return []
        try:
            return self._infer(frames)
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            if len(frames) > 1:
                stats.oom_batch_reductions += 1
                half = len(frames) // 2
                log(f"CUDA OOM — splitting batch {len(frames)} -> {half}+{len(frames) - half}.")
                return self.count_people(frames[:half], stats) + self.count_people(frames[half:], stats)
            log("CUDA OOM on a single frame — moving detector to CPU for the rest of the run.")
            stats.cpu_fallback = True
            self.device = "cpu"
            self.use_fp16 = False
            self.model = self.model.float().to("cpu")
            torch.cuda.empty_cache()
            return self._infer(frames)

    def _infer(self, frames: list["Any"]) -> list[int]:
        import torch

        assert self.model is not None and self.processor is not None, "call load() first"
        inputs = self.processor(images=frames, return_tensors="pt")
        pixel_values = inputs["pixel_values"].to(self.device)
        if self.use_fp16:
            pixel_values = pixel_values.half()
        with torch.inference_mode():
            outputs = self.model(pixel_values=pixel_values)
        target_sizes = torch.tensor([frame.shape[:2] for frame in frames])
        results = self.processor.post_process_object_detection(
            outputs,
            threshold=self.config.confidence,
            target_sizes=target_sizes,
        )
        counts: list[int] = []
        for result in results:
            labels = result["labels"].tolist()
            counts.append(sum(1 for label in labels if int(label) in self.person_label_ids))
        return counts

    def close(self) -> None:
        """Release model + VRAM promptly (process exit would too; this is tidier)."""
        self.model = None
        self.processor = None
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass


# ---------------------------------------------------------------------------
# Segmentation logic (pure functions — no I/O, deterministic, testable)
# ---------------------------------------------------------------------------


def median_smooth(counts: list[int], window: int) -> list[int]:
    """Centered rolling median; kills single-sample detector flicker."""
    if window <= 1 or len(counts) <= 2:
        return list(counts)
    half = window // 2
    smoothed: list[int] = []
    for index in range(len(counts)):
        lo = max(0, index - half)
        hi = min(len(counts), index + half + 1)
        neighborhood = sorted(counts[lo:hi])
        smoothed.append(neighborhood[len(neighborhood) // 2])
    return smoothed


def _run_length_encode(flags: list[bool]) -> list[tuple[bool, int, int]]:
    """RLE as ``(value, start_index, length)`` tuples."""
    runs: list[tuple[bool, int, int]] = []
    start = 0
    for index in range(1, len(flags) + 1):
        if index == len(flags) or flags[index] != flags[start]:
            runs.append((flags[start], start, index - start))
            start = index
    return runs


def close_flicker_gaps(flags: list[bool], max_gap_samples: int) -> list[bool]:
    """Morphological closing in both directions.

    Any run shorter than ``max_gap_samples`` that sits BETWEEN two runs of the
    opposite value is inverted — this absorbs both a briefly-missed person
    inside a session and a phantom second person inside a gap. Runs at the
    edges of the signal are left alone (no context to justify flipping them).
    """
    if max_gap_samples <= 0 or len(flags) < 3:
        return list(flags)
    result = list(flags)
    runs = _run_length_encode(result)
    for run_index in range(1, len(runs) - 1):
        value, start, length = runs[run_index]
        if length <= max_gap_samples:
            for index in range(start, start + length):
                result[index] = not value
    return result


def build_session_ranges(
    counts: list[int],
    video_duration: float,
    config: SegmenterConfig,
) -> tuple[list[dict[str, float]], list[dict[str, Any]]]:
    """Hysteresis state machine over per-sample person counts.

    Returns ``(clip_ranges, transitions)`` where transitions document every
    accepted state change (for the debug payload / tuning experiments).

    Edge cases handled:
    * Video starts mid-session (people already present at t=0) -> the first
      clip starts at 0.
    * Video ends mid-session -> the final clip is closed at video_duration.
    * Detector flicker -> pre-smoothed by the caller AND runs shorter than the
      confirmation windows are absorbed here.
    * A low-person dip shorter than ``end_after_seconds`` never splits a
      session; a crowd blip shorter than ``start_after_seconds`` never opens
      one.
    """
    smoothed = median_smooth(counts, config.median_window)
    in_session_raw = [count >= config.min_people for count in smoothed]
    in_session = close_flicker_gaps(
        in_session_raw,
        config.seconds_to_samples(config.flicker_tolerance_seconds),
    )

    start_confirm = config.seconds_to_samples(config.start_after_seconds)
    end_confirm = config.seconds_to_samples(config.end_after_seconds)
    dt = config.sample_dt

    ranges: list[dict[str, float]] = []
    transitions: list[dict[str, Any]] = []
    active_start: float | None = None

    runs = _run_length_encode(in_session)
    for value, start_index, length in runs:
        run_start_time = start_index * dt
        if active_start is None:
            # IDLE: only a sufficiently long high-person run opens a session.
            if value and length >= start_confirm:
                active_start = run_start_time
                transitions.append(
                    {"at": round(run_start_time, 3), "event": "session_start", "run_samples": length}
                )
        else:
            # ACTIVE: only a sufficiently long low-person run closes it. The
            # session ends when the low run BEGAN (that is when people left).
            if not value and length >= end_confirm:
                ranges.append({"start": active_start, "end": run_start_time})
                transitions.append(
                    {"at": round(run_start_time, 3), "event": "session_end", "run_samples": length}
                )
                active_start = None

    if active_start is not None:
        # Video ended while a session was still active.
        ranges.append({"start": active_start, "end": video_duration})
        transitions.append({"at": round(video_duration, 3), "event": "session_end_at_eof"})

    # Padding + clamping + minimum-duration filtering, mirroring the bell
    # detector's offset semantics.
    padded: list[dict[str, float]] = []
    for clip_range in ranges:
        start = max(0.0, min(video_duration, clip_range["start"] - config.start_offset_seconds))
        end = max(0.0, min(video_duration, clip_range["end"] + config.end_offset_seconds))
        if end - start >= config.min_clip_seconds:
            padded.append({"start": round(start, 3), "end": round(end, 3)})

    # Overlap guard: end-padding of clip N must not spill past the (padded)
    # start of clip N+1.
    for index in range(len(padded) - 1):
        if padded[index]["end"] > padded[index + 1]["start"]:
            padded[index]["end"] = padded[index + 1]["start"]
    padded = [r for r in padded if r["end"] - r["start"] >= config.min_clip_seconds]

    for index, clip_range in enumerate(padded, start=1):
        clip_range["student_index"] = index
    return padded, transitions


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Detect OSCE student session clip ranges from person presence "
            "(RT-DETR person detection over sparsely sampled frames)."
        )
    )
    parser.add_argument("--video", required=True, help="Path to the source video file")
    parser.add_argument(
        "--video-duration",
        required=True,
        type=float,
        help="Full source video duration in seconds (from ffprobe, supplied by the caller)",
    )
    parser.add_argument("--output", default="", help="Optional path to also write the JSON payload to")
    parser.add_argument(
        "--sample-fps",
        type=float,
        default=read_float_env("HUMAN_SEGMENTS_SAMPLE_FPS", 1.0),
        help="Frames analysed per second of video (default 1.0 — do NOT run every frame)",
    )
    parser.add_argument(
        "--min-people",
        type=int,
        default=read_int_env("HUMAN_SEGMENTS_MIN_PEOPLE", 2),
        help="People required on screen for a session to count as active (default 2)",
    )
    parser.add_argument(
        "--end-after-seconds",
        type=float,
        default=read_float_env("HUMAN_SEGMENTS_END_AFTER_SECONDS", 8.0),
        help=(
            "Continuous seconds below --min-people that end a session "
            "(default 8.0 s ~= 240 frames @ 30 fps)"
        ),
    )
    parser.add_argument(
        "--start-after-seconds",
        type=float,
        default=read_float_env("HUMAN_SEGMENTS_START_AFTER_SECONDS", 4.0),
        help="Continuous seconds at/above --min-people that start a session (default 4.0)",
    )
    parser.add_argument(
        "--flicker-tolerance-seconds",
        type=float,
        default=read_float_env("HUMAN_SEGMENTS_FLICKER_TOLERANCE_SECONDS", 2.0),
        help="Contradictory blips shorter than this are absorbed (detector flicker guard)",
    )
    parser.add_argument(
        "--median-window",
        type=int,
        default=read_int_env("HUMAN_SEGMENTS_MEDIAN_WINDOW", 3),
        help="Rolling-median window (samples) applied to person counts (default 3)",
    )
    parser.add_argument("--start-offset", type=float, default=0.0, help="Seconds to pad before each clip start")
    parser.add_argument("--end-offset", type=float, default=2.0, help="Seconds to pad after each clip end")
    parser.add_argument("--min-clip-seconds", type=float, default=0.5, help="Minimum clip duration to keep")
    parser.add_argument(
        "--confidence",
        type=float,
        default=read_float_env("HUMAN_SEGMENTS_CONFIDENCE", 0.5),
        help="Detection score threshold for counting a person (default 0.5)",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("HUMAN_SEGMENTS_MODEL") or DEFAULT_MODEL,
        help=f"HuggingFace object-detection checkpoint (default {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--device",
        choices=["auto", "cuda", "cpu"],
        default=os.getenv("HUMAN_SEGMENTS_DEVICE") or "auto",
        help="Inference device (default auto: cuda when available)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=read_int_env("HUMAN_SEGMENTS_BATCH_SIZE", 8),
        help="Frames per inference batch (auto-halved on CUDA OOM; default 8)",
    )
    parser.add_argument(
        "--decode-width",
        type=int,
        default=640,
        help="Width frames are pre-scaled to by ffmpeg before inference (default 640)",
    )
    parser.add_argument(
        "--dump-samples",
        default="",
        help="Optional path to write per-sample person counts (JSON) for threshold tuning",
    )
    parser.add_argument(
        "--allow-fallback-full-video",
        action="store_true",
        help=(
            "When no session boundaries are found, emit one clip covering the "
            "whole video instead of an empty list"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    video_path = Path(args.video).expanduser().resolve()
    if not video_path.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")
    video_duration = float(args.video_duration)
    if video_duration <= 0:
        raise ValueError("--video-duration must be a positive number of seconds.")

    segmenter_config = SegmenterConfig(
        sample_fps=max(0.1, float(args.sample_fps)),
        min_people=max(1, int(args.min_people)),
        start_after_seconds=max(0.0, float(args.start_after_seconds)),
        end_after_seconds=max(0.0, float(args.end_after_seconds)),
        flicker_tolerance_seconds=max(0.0, float(args.flicker_tolerance_seconds)),
        median_window=max(1, int(args.median_window)),
        start_offset_seconds=max(0.0, float(args.start_offset)),
        end_offset_seconds=max(0.0, float(args.end_offset)),
        min_clip_seconds=max(0.0, float(args.min_clip_seconds)),
    )
    detector_config = DetectorConfig(
        model_id=str(args.model),
        device=str(args.device),
        confidence=min(0.99, max(0.05, float(args.confidence))),
        batch_size=max(1, int(args.batch_size)),
        decode_width=max(160, int(args.decode_width)),
    )

    ffmpeg_bin = resolve_binary("FFMPEG_BIN", "ffmpeg")
    ffprobe_bin = resolve_binary("FFPROBE_BIN", "ffprobe")
    source_width, source_height = probe_video_dimensions(ffprobe_bin, video_path)
    out_width = min(detector_config.decode_width, source_width)
    # Keep aspect ratio; even dimensions keep every scaler/codec path happy.
    out_height = max(2, round(source_height * out_width / source_width / 2) * 2)
    out_width = max(2, out_width // 2 * 2)

    expected_samples = int(video_duration * segmenter_config.sample_fps) + 1
    log(
        f"Sampling {video_path.name} at {segmenter_config.sample_fps} fps "
        f"({source_width}x{source_height} -> {out_width}x{out_height}, "
        f"~{expected_samples} frames expected)."
    )

    stats = DetectionStats(effective_batch_size=detector_config.batch_size)
    counter = PersonCounter(detector_config)
    counter.load()

    counts: list[int] = []
    batch: list[Any] = []
    decode_started = time.perf_counter()
    inference_seconds = 0.0
    try:
        for frame in iter_sampled_frames(
            ffmpeg_bin,
            video_path,
            sample_fps=segmenter_config.sample_fps,
            out_width=out_width,
            out_height=out_height,
        ):
            batch.append(frame)
            if len(batch) >= detector_config.batch_size:
                inference_started = time.perf_counter()
                counts.extend(counter.count_people(batch, stats))
                inference_seconds += time.perf_counter() - inference_started
                batch = []
                if len(counts) % 120 < detector_config.batch_size:
                    done_pct = min(100.0, 100.0 * len(counts) / max(1, expected_samples))
                    log(f"Analysed {len(counts)} frames (~{done_pct:.0f}%).")
        if batch:
            inference_started = time.perf_counter()
            counts.extend(counter.count_people(batch, stats))
            inference_seconds += time.perf_counter() - inference_started
    finally:
        counter.close()

    stats.decode_seconds = round(time.perf_counter() - decode_started - inference_seconds, 3)
    stats.inference_seconds = round(inference_seconds, 3)
    stats.sampled_frames = len(counts)

    if not counts:
        raise RuntimeError(
            "No frames could be decoded from the video — confirm the file is a "
            "readable video (ffmpeg could not extract any sampled frames)."
        )

    for count in counts:
        key = str(count)
        stats.person_count_histogram[key] = stats.person_count_histogram.get(key, 0) + 1
    log(f"Person-count histogram: {stats.person_count_histogram}")

    clip_ranges, transitions = build_session_ranges(counts, video_duration, segmenter_config)
    used_trigger = "person_presence"
    if not clip_ranges and args.allow_fallback_full_video:
        log("No session boundaries found — falling back to one full-video clip.")
        clip_ranges = [{"start": 0.0, "end": video_duration, "student_index": 1}]
        used_trigger = "fallback_full_video"

    log(f"Detected {len(clip_ranges)} session clip(s) via {used_trigger}.")

    if args.dump_samples:
        samples_path = Path(args.dump_samples).expanduser().resolve()
        samples_path.parent.mkdir(parents=True, exist_ok=True)
        samples_payload = [
            {"t": round(index * segmenter_config.sample_dt, 3), "people": count}
            for index, count in enumerate(counts)
        ]
        samples_path.write_text(json.dumps(samples_payload), encoding="utf-8")
        log(f"Wrote {len(samples_payload)} per-sample counts to {samples_path}.")

    payload: dict[str, Any] = {
        "detector": DETECTOR_NAME,
        "detector_mode": "person_presence",
        "used_trigger": used_trigger,
        "video_file": str(video_path),
        "video_duration": video_duration,
        "model": counter.resolved_model_id,
        "device": counter.device,
        "confidence": detector_config.confidence,
        "sample_fps": segmenter_config.sample_fps,
        "sampled_frames": stats.sampled_frames,
        "min_people": segmenter_config.min_people,
        "start_after_seconds": segmenter_config.start_after_seconds,
        "end_after_seconds": segmenter_config.end_after_seconds,
        "flicker_tolerance_seconds": segmenter_config.flicker_tolerance_seconds,
        "median_window": segmenter_config.median_window,
        "start_offset": segmenter_config.start_offset_seconds,
        "end_offset": segmenter_config.end_offset_seconds,
        "min_clip_seconds": segmenter_config.min_clip_seconds,
        "student_count": len(clip_ranges),
        "clip_ranges": clip_ranges,
        "transitions": transitions,
        "person_count_histogram": stats.person_count_histogram,
        "debug": {
            "decode_seconds": stats.decode_seconds,
            "inference_seconds": stats.inference_seconds,
            "batch_size": detector_config.batch_size,
            "oom_batch_reductions": stats.oom_batch_reductions,
            "cpu_fallback": stats.cpu_fallback,
            "decode_resolution": f"{out_width}x{out_height}",
            "source_resolution": f"{source_width}x{source_height}",
        },
    }

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"Wrote payload to {output_path}.")

    # stdout carries exactly one JSON document — the machine-readable contract.
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
