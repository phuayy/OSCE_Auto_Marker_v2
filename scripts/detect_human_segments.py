#!/usr/bin/env python3
"""Detect OSCE session boundaries from *person presence* using RT-DETR.

Vision-based alternative to ``detect_bell_segments.py``. The script samples
video frames at a low rate (default 1 frame/second), counts people per frame
with an RT-DETR object detector, and derives student clip ranges from
sustained changes in person count:

* A session is ACTIVE while at least ``--min-people`` (default 2: the student
  plus the patient/examiner) are visible. A detection only counts as a person
  when its box is at least ``--min-box-height-ratio`` of the frame height —
  the filter that stops a hand or shoulder intruding at the edge of the frame
  from being counted as a whole extra person (0, the default, disables it).
* Confirmed sessions shorter than ``--min-session-seconds`` are discarded — a
  second, independent guard against a burst of false positives opening a clip.
* ``--preset`` bundles those three numbers into the camera scenarios the
  operator actually picks from (pair / pair_strict / solo / custom); see
  ``fastapi_backend/app/pipeline/person_presets.py`` for the measurements
  behind each. Explicit flags override whatever the preset chose.
* Boundaries are confirmed with a tolerant **N-of-M window** (the standard
  debounce for noisy boolean sensors): a session STARTS at the first sample
  that crossed the threshold once a full ``--start-after-seconds`` window
  contains at most ``--flicker-tolerance-seconds`` disagreeing samples
  (defaults: 50-sample window, 10 tolerated misclassifications @ 1 fps), and
  ENDS symmetrically via ``--end-after-seconds``. A short detector dropout can
  never split a session; a short crowd blip can never open one.

Besides the confirmed session clips the script also emits a full **timeline
partition**: an ordered, gapless cover of ``[0, video_duration]`` where every
segment is either a confirmed ``session`` or an ``intermission`` (empty room /
single person walking about). Intermissions let the UI show — greyed out —
what happened between sessions instead of silently dropping that footage.

Detection can run over **N parallel worker processes** (``--workers`` /
``HUMAN_SEGMENTS_WORKERS``, default 1). The video is split into contiguous
time-chunks aligned to the sample grid; each worker loads its own model,
detects over its chunk, and the parent merges the per-chunk person counts back
into one global timeline before segmenting ONCE — a session straddling a chunk
boundary is still detected. On a single small GPU (e.g. 4 GB RTX 3050) CUDA
serializes across processes, so >1 worker costs 2x model VRAM for ~no speedup;
the default stays 1 and parallelism is opt-in for multi-GPU / CPU hosts. The
per-process OOM batch-halving/CPU-fallback keeps oversubscribed runs degrading
instead of dying.

Model selection (researched for an RTX 3050 Laptop GPU, 4 GB VRAM, sharing the
machine with WhisperX ``large-v3``):

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
stdout carries exactly one JSON document. ``clip_ranges`` holds the confirmed
sessions ``[{"start": float, "end": float, "student_index": int}, ...]`` (the
shape ``MediaPipeline.normalize_clip_ranges`` consumes); ``timeline_segments``
holds the full partition ``[{"start", "end", "kind", "person_count",
"student_index"?}, ...]``.

Usage:
    python scripts/detect_human_segments.py \
        --video storage/input/videos/<session>.mp4 --video-duration 3600 \
        [--sample-fps 1.0] [--workers 1] [--output out.json]

    python scripts/detect_human_segments.py --self-check   # pure-math asserts
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm_bootstrap import read_int_env  # noqa: E402

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


def _optional_float_env(name: str) -> float | None:
    """Float from the environment, or None when unset/unparseable.

    Distinct from ``read_float_env``: preset-backed knobs need "the operator
    said nothing" to stay distinguishable from "the operator said 0".
    """
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def log(message: str) -> None:
    """Progress/diagnostic line. stderr only — stdout is reserved for the JSON
    contract (CommandRunner streams stderr into the live SSE log)."""
    print(f"[human-segments] {message}", file=sys.stderr, flush=True)


# The occupancy preset table is shared with the backend rather than duplicated:
# the API validates against it, the upload screen renders from it, and this
# script resolves ``--preset`` with it, so "solo" cannot come to mean two
# different things in two places. Only stdlib is imported transitively (the
# module deliberately depends on nothing from the app), and the repo layout
# guarantees the path — the same trick scripts/llm_bootstrap.py uses.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent / "fastapi_backend"
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
from app.pipeline import person_presets  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "PekingU/rtdetr_v2_r18vd"
# Ordered fallbacks when the default model id cannot be loaded (e.g. an older
# transformers without RT-DETRv2 support). All are person-capable COCO models.
FALLBACK_MODELS = ("PekingU/rtdetr_r18vd", "PekingU/rtdetr_r50vd")

DETECTOR_NAME = "osce-human-presence-rtdetr-v2"

SESSION_KIND = "session"
INTERMISSION_KIND = "intermission"


@dataclass(frozen=True)
class SegmenterConfig:
    """Pure segmentation parameters (no I/O concerns) — unit-testable.

    Defaults are the values validated against real OSCE footage with the
    debug harnesses (``debug_scripts/rt_detr*.py``): a 50-sample confirmation
    window tolerating 10 misclassified samples, at 1 sample/second.
    """

    sample_fps: float = 1.0
    min_people: int = 2
    start_after_seconds: float = 50.0
    end_after_seconds: float = 50.0
    flicker_tolerance_seconds: float = 10.0
    start_offset_seconds: float = 0.0
    end_offset_seconds: float = 2.0
    min_clip_seconds: float = 0.5
    # Shortest confirmed session kept (0 = keep every confirmed session). Unlike
    # min_clip_seconds — a sliver guard measured in fractions of a second — this
    # encodes "an OSCE station lasts minutes", which is what makes a burst of
    # false detections fail to produce a clip.
    min_session_seconds: float = 0.0

    @property
    def sample_dt(self) -> float:
        return 1.0 / max(0.01, self.sample_fps)

    def seconds_to_samples(self, seconds: float) -> int:
        return max(1, round(seconds / self.sample_dt))


@dataclass(frozen=True)
class DetectorConfig:
    model_id: str = DEFAULT_MODEL
    device: str = "auto"  # auto | cuda | cpu
    confidence: float = 0.7
    batch_size: int = 8
    decode_width: int = 640  # ffmpeg pre-scale; the processor re-sizes anyway
    # Smallest box height, as a fraction of frame height, that counts as a
    # person. 0 disables the gate (historic behaviour). See person_presets for
    # the measured separation between real people and intruding limbs.
    min_box_height_ratio: float = 0.0


@dataclass
class DetectionStats:
    """Runtime diagnostics surfaced in the output payload for tuning."""

    decode_seconds: float = 0.0
    inference_seconds: float = 0.0
    sampled_frames: int = 0
    oom_batch_reductions: int = 0
    cpu_fallback: bool = False
    effective_batch_size: int = 0
    workers: int = 1
    padded_samples: int = 0
    person_count_histogram: dict[str, int] = field(default_factory=dict)
    # Detections the confidence threshold accepted but the height gate rejected.
    # Surfaced in the payload so a mis-tuned gate is diagnosable from one run
    # instead of guessed at.
    rejected_small_boxes: int = 0


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
            # .copy() detaches from the immutable `bytes` read buffer — np.frombuffer's
            # array is read-only (a view over `chunk`), which the fast image processor's
            # zero-copy torch conversion warns about (UB if anything ever writes into it).
            yield np.frombuffer(chunk, dtype=np.uint8).reshape((out_height, out_width, 3)).copy()
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


def iter_chunk_frames(
    ffmpeg_bin: str,
    video_path: Path,
    *,
    start_seconds: float,
    duration_seconds: float,
    sample_fps: float,
    out_width: int,
    out_height: int,
) -> Iterator["Any"]:
    """Yield RGB numpy frames for one time-chunk (multi-worker decoding).

    ``-ss`` before ``-i`` is frame-accurate in modern ffmpeg (decodes from the
    previous keyframe, discards up to the seek point) and resets output
    timestamps, so the ``fps=`` grid starts exactly at ``start_seconds`` —
    matching the global sample grid when start_seconds is a multiple of the
    sample interval. Unlike ``iter_sampled_frames``, an early generator close
    kills ffmpeg instead of raising (workers stop reading once their planned
    sample count is reached).
    """
    import numpy as np

    frame_bytes = out_width * out_height * 3
    args = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel", "error",
        "-ss", f"{start_seconds:.6f}",
        "-i", str(video_path),
        "-t", f"{duration_seconds:.6f}",
        "-vf", f"fps={sample_fps},scale={out_width}:{out_height}",
        "-pix_fmt", "rgb24",
        "-f", "rawvideo",
        "pipe:1",
    ]
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None
    try:
        while True:
            chunk = process.stdout.read(frame_bytes)
            if not chunk or len(chunk) < frame_bytes:
                break
            yield np.frombuffer(chunk, dtype=np.uint8).reshape((out_height, out_width, 3)).copy()
    finally:
        killed = process.poll() is None  # still running => generator closed early
        if killed:
            process.kill()
        process.stdout.close()
        stderr_tail = b""
        if process.stderr is not None:
            stderr_tail = process.stderr.read() or b""
            process.stderr.close()
        return_code = process.wait()
        if return_code not in (0, None) and not killed:
            raise RuntimeError(
                f"ffmpeg chunk decoding failed (exit {return_code}): "
                f"{stderr_tail.decode('utf-8', 'replace').strip()[:400]}"
            )


# ---------------------------------------------------------------------------
# Person detection (RT-DETR via HuggingFace transformers)
# ---------------------------------------------------------------------------


def count_valid_people(
    boxes: list[tuple[float, float, float, float]],
    frame_height: float,
    min_box_height_ratio: float,
) -> tuple[int, int]:
    """Count person boxes that clear the height gate. Returns ``(kept, rejected)``.

    Pure, so the gate can be tested against recorded detections without a GPU.
    ``boxes`` are ``(x0, y0, x1, y1)`` in pixels of the analysed frame, already
    filtered to the person class and to the confidence threshold.

    A box is rejected when it is too *short* relative to the frame — the one
    measurement that separates a whole person from the hand, forearm or
    shoulder of somebody standing outside the shot. Width is deliberately not
    tested: a person seated side-on is legitimately wide, while a raised arm
    across the lens is legitimately narrow.
    """
    if min_box_height_ratio <= 0.0 or frame_height <= 0:
        return len(boxes), 0
    kept = 0
    for _x0, y0, _x1, y1 in boxes:
        if (max(0.0, y1 - y0) / frame_height) >= min_box_height_ratio:
            kept += 1
    return kept, len(boxes) - kept


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
                "uv sync"
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
            # Try the local HF cache first: from_pretrained() otherwise always
            # does a network HEAD to check for a newer revision, even when the
            # checkpoint is fully cached — pure overhead, and a slow/flaky path
            # to huggingface.co turns it into a 30s+ retry stall per worker.
            # Falls back to the normal (network-allowed) load on a cache miss.
            for local_only in (True, False):
                try:
                    log(
                        f"Loading detector {model_id} on {self.device} (fp16={self.use_fp16}, "
                        f"local_files_only={local_only})..."
                    )
                    # use_fast=True selects the torchvision-backed RTDetrImageProcessorFast
                    # instead of the PIL-backed slow processor — same numpy-array input
                    # contract and output shape, ~2-3x faster preprocessing, negligible
                    # (~1e-7) numeric difference. Silences the "slow image processor"
                    # deprecation warning (transformers defaults to fast from v4.52).
                    self.processor = AutoImageProcessor.from_pretrained(
                        model_id, use_fast=True, local_files_only=local_only
                    )
                    model = AutoModelForObjectDetection.from_pretrained(
                        model_id, local_files_only=local_only
                    )
                    self.resolved_model_id = model_id
                    break
                except Exception as error:  # noqa: BLE001 — surface the last cause below
                    last_error = error
                    model = None
            if model is not None:
                break
            log(f"Could not load {model_id}: {last_error}")
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
            return self._infer(frames, stats)
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
            return self._infer(frames, stats)

    def _infer(self, frames: list["Any"], stats: DetectionStats) -> list[int]:
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
        for frame, result in zip(frames, results):
            frame_height = float(frame.shape[0])
            person_boxes = [
                tuple(box)
                for label, box in zip(result["labels"].tolist(), result["boxes"].tolist())
                if int(label) in self.person_label_ids
            ]
            kept, rejected = count_valid_people(
                person_boxes, frame_height, self.config.min_box_height_ratio
            )
            stats.rejected_small_boxes += rejected
            counts.append(kept)
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


def _window_confirms(flags: list[bool], start: int, window: int, tolerance: int, want: bool) -> str:
    """Check ``flags[start:start+window]`` against ``want``.

    Returns "confirmed" (<= tolerance disagreements), "failed" (too many),
    or "insufficient" (fewer than ``window`` samples remain before EOF).
    """
    end = start + window
    if end > len(flags):
        return "insufficient"
    bad = sum(1 for flag in flags[start:end] if flag != want)
    return "confirmed" if bad <= tolerance else "failed"


def build_tolerant_session_ranges(
    counts: list[int],
    video_duration: float,
    *,
    min_people: int,
    sample_dt: float,
    start_window_samples: int,
    start_tolerance_samples: int,
    end_window_samples: int,
    end_tolerance_samples: int,
    start_offset_seconds: float,
    end_offset_seconds: float,
    min_clip_seconds: float,
    min_session_seconds: float = 0.0,
) -> tuple[list[dict], list[dict]]:
    """Tolerant N-of-M hysteresis over per-sample person counts.

    Confirms a state transition once a fixed-length sample window has AT MOST
    ``tolerance`` samples that disagree with the target state ("at most 10 bad
    samples out of a 50-sample window"). Symmetric: the exact same check
    confirms both session start and session end, so a clip's cut-off tolerates
    detector dropouts the same way its start does.

    Anchors each confirmed clip to the FIRST sample that individually crossed
    ``min_people`` (or dropped below it), not the sample where the
    confirmation window completed — "take the first frame identified as
    2 people and start the clip from there".

    Edge cases:
    * Video starts mid-session -> first clip starts at 0.
    * Video ends mid-session -> final clip closes at ``video_duration``.
    * A window that cannot complete before EOF never confirms (state holds).
    """
    flags = [count >= min_people for count in counts]
    n = len(flags)
    ranges: list[dict] = []
    transitions: list[dict] = []
    state = "idle"
    anchor: int | None = None
    session_start_index = 0
    index = 0
    while index < n:
        if state == "idle":
            if anchor is None:
                if not flags[index]:
                    index += 1
                    continue
                anchor = index
            result = _window_confirms(flags, anchor, start_window_samples, start_tolerance_samples, True)
            if result == "confirmed":
                session_start_index = anchor
                transitions.append(
                    {"at": round(anchor * sample_dt, 3), "event": "session_start", "run_samples": start_window_samples}
                )
                state = "active"
                index = anchor + start_window_samples
                anchor = None
            elif result == "failed":
                index = anchor + 1
                anchor = None
            else:  # insufficient samples left before EOF — this attempt can never confirm
                break
        else:  # active
            if anchor is None:
                if flags[index]:
                    index += 1
                    continue
                anchor = index
            result = _window_confirms(flags, anchor, end_window_samples, end_tolerance_samples, False)
            if result == "confirmed":
                ranges.append({"start": session_start_index * sample_dt, "end": anchor * sample_dt})
                transitions.append(
                    {"at": round(anchor * sample_dt, 3), "event": "session_end", "run_samples": end_window_samples}
                )
                state = "idle"
                index = anchor + end_window_samples
                anchor = None
            elif result == "failed":
                index = anchor + 1
                anchor = None
            else:
                break

    if state == "active":
        ranges.append({"start": session_start_index * sample_dt, "end": video_duration})
        transitions.append({"at": round(video_duration, 3), "event": "session_end_at_eof"})

    # Padding + clamping + minimum-duration filtering (offset padding, overlap
    # guard, min duration) — mirrors the bell detector's offset semantics.
    padded: list[dict] = []
    for clip_range in ranges:
        start = max(0.0, min(video_duration, clip_range["start"] - start_offset_seconds))
        end = max(0.0, min(video_duration, clip_range["end"] + end_offset_seconds))
        if end - start >= min_clip_seconds:
            padded.append({"start": round(start, 3), "end": round(end, 3)})
    for i in range(len(padded) - 1):
        if padded[i]["end"] > padded[i + 1]["start"]:
            padded[i]["end"] = padded[i + 1]["start"]
    padded = [r for r in padded if r["end"] - r["start"] >= min_clip_seconds]
    # Duration guard, applied BEFORE numbering so the surviving clips are still
    # Student 1..N with no holes. A short confirmed range is either a detector
    # artefact or somebody tidying the room; either way it is not a station.
    if min_session_seconds > 0:
        dropped = [r for r in padded if r["end"] - r["start"] < min_session_seconds]
        if dropped:
            log(
                f"Discarding {len(dropped)} confirmed range(s) shorter than "
                f"{min_session_seconds:g}s: "
                + ", ".join(f"{r['start']:.0f}-{r['end']:.0f}s" for r in dropped[:8])
                + ("..." if len(dropped) > 8 else "")
            )
        padded = [r for r in padded if r["end"] - r["start"] >= min_session_seconds]
    for i, clip_range in enumerate(padded, start=1):
        clip_range["student_index"] = i
    return padded, transitions


def _majority_person_count(counts: list[int], start_seconds: float, end_seconds: float, sample_dt: float) -> int:
    """Most common sampled person count inside ``[start, end]`` (ties -> larger)."""
    first = max(0, math.ceil(start_seconds / sample_dt - 1e-9))
    last = min(len(counts) - 1, int(end_seconds / sample_dt + 1e-9))
    if last < first:
        return 0
    tally: dict[int, int] = {}
    for value in counts[first : last + 1]:
        tally[value] = tally.get(value, 0) + 1
    return max(tally.items(), key=lambda item: (item[1], item[0]))[0]


def build_timeline_segments(
    counts: list[int],
    clip_ranges: list[dict],
    video_duration: float,
    sample_dt: float,
) -> list[dict]:
    """Ordered, gapless partition of ``[0, video_duration]``.

    Confirmed sessions become ``kind="session"`` segments (carrying their
    ``student_index``); everything between them becomes ``kind="intermission"``
    segments so the UI can show — greyed out — the empty-room / one-person
    stretches instead of silently dropping them. ``person_count`` is the
    majority sampled count within the segment (0 = empty room, 1 = one person).
    """
    segments: list[dict] = []

    def add_intermission(start: float, end: float) -> None:
        if end - start <= 1e-6:
            return
        segments.append(
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "kind": INTERMISSION_KIND,
                "person_count": _majority_person_count(counts, start, end, sample_dt),
            }
        )

    cursor = 0.0
    for clip in clip_ranges:
        add_intermission(cursor, clip["start"])
        segments.append(
            {
                "start": clip["start"],
                "end": clip["end"],
                "kind": SESSION_KIND,
                "person_count": _majority_person_count(counts, clip["start"], clip["end"], sample_dt),
                "student_index": clip.get("student_index"),
            }
        )
        cursor = clip["end"]
    add_intermission(cursor, video_duration)
    return segments


# ---------------------------------------------------------------------------
# Multi-process detection: chunk planning + worker + orchestration
# ---------------------------------------------------------------------------


def split_sample_indices(total_samples: int, workers: int) -> list[tuple[int, int]]:
    """Split ``[0, total_samples)`` into up to ``workers`` contiguous near-equal
    half-open spans. Never returns an empty span; fewer spans than requested
    when the video is shorter than the worker count."""
    if total_samples <= 0:
        return []
    workers = max(1, min(workers, total_samples))
    base, extra = divmod(total_samples, workers)
    spans: list[tuple[int, int]] = []
    start = 0
    for i in range(workers):
        size = base + (1 if i < extra else 0)
        spans.append((start, start + size))
        start += size
    return spans


def run_chunk(task: dict) -> dict:
    """Decode + detect one time-chunk (runs in a spawned child process).

    Returns per-sample person counts plus timing/diagnostic fields; frames
    never cross the process boundary.
    """
    wall_started = time.perf_counter()
    worker_id = int(task["worker"])
    start_index = int(task["start_index"])
    end_index = int(task["end_index"])
    expected = end_index - start_index
    sample_dt = float(task["sample_dt"])

    def wlog(message: str) -> None:
        log(f"[worker {worker_id}] {message}")

    detector_config = DetectorConfig(
        model_id=task["model_id"],
        device=task["device"],
        confidence=task["confidence"],
        batch_size=task["batch_size"],
        decode_width=task["decode_width"],
        min_box_height_ratio=task["min_box_height_ratio"],
    )
    stats = DetectionStats(effective_batch_size=detector_config.batch_size)
    counter = PersonCounter(detector_config)
    load_started = time.perf_counter()
    counter.load()
    load_seconds = time.perf_counter() - load_started
    wlog(
        f"model ready in {load_seconds:.1f}s ({counter.device}); "
        f"samples {start_index}..{end_index - 1} (t={start_index * sample_dt:.0f}s..)"
    )

    counts: list[int] = []
    batch: list[Any] = []
    decode_started = time.perf_counter()
    inference_seconds = 0.0
    try:
        for frame in iter_chunk_frames(
            task["ffmpeg_bin"],
            Path(task["video_path"]),
            start_seconds=start_index * sample_dt,
            duration_seconds=expected * sample_dt,
            sample_fps=task["sample_fps"],
            out_width=task["out_width"],
            out_height=task["out_height"],
        ):
            batch.append(frame)
            if len(batch) >= detector_config.batch_size:
                inference_started = time.perf_counter()
                counts.extend(counter.count_people(batch, stats))
                inference_seconds += time.perf_counter() - inference_started
                batch = []
                if len(counts) % 120 < detector_config.batch_size:
                    wlog(f"analysed {len(counts)}/{expected} samples")
            if len(counts) + len(batch) >= expected:
                break  # planned sample count reached — stop decoding
        if batch:
            inference_started = time.perf_counter()
            counts.extend(counter.count_people(batch, stats))
            inference_seconds += time.perf_counter() - inference_started
    finally:
        counter.close()

    counts = counts[:expected]  # safety cap; padding for short chunks is the parent's job
    wlog(f"done: {len(counts)}/{expected} samples in {time.perf_counter() - wall_started:.1f}s")
    return {
        "chunk_index": int(task["chunk_index"]),
        "start_index": start_index,
        "end_index": end_index,
        "counts": counts,
        "decode_seconds": time.perf_counter() - decode_started - inference_seconds,
        "inference_seconds": inference_seconds,
        "oom_batch_reductions": stats.oom_batch_reductions,
        "rejected_small_boxes": stats.rejected_small_boxes,
        "cpu_fallback": stats.cpu_fallback,
        "device": counter.device,
        "model": counter.resolved_model_id,
    }


def detect_counts_parallel(
    *,
    workers: int,
    total_samples: int,
    video_path: Path,
    ffmpeg_bin: str,
    sample_fps: float,
    sample_dt: float,
    out_width: int,
    out_height: int,
    detector_config: DetectorConfig,
    stats: DetectionStats,
) -> tuple[list[int], str, str]:
    """Fan detection out over N worker processes and merge the count timeline.

    Segmentation runs ONCE over the merged counts in the parent, so a session
    straddling a chunk boundary is still detected. Chunks that decode short
    (EOF rounding) are padded by repeating their last count so the global
    timeline stays index-aligned. Returns ``(counts, device, model)``.
    """
    spans = split_sample_indices(total_samples, workers)
    if not spans:
        raise RuntimeError("Video too short to sample any frames.")
    for i, (span_start, span_end) in enumerate(spans):
        log(f"chunk {i}: samples {span_start}..{span_end - 1} (t={span_start * sample_dt:.0f}s..)")

    tasks = [
        {
            "chunk_index": i,
            "worker": i,
            "start_index": span_start,
            "end_index": span_end,
            "sample_dt": sample_dt,
            "sample_fps": sample_fps,
            "video_path": str(video_path),
            "ffmpeg_bin": ffmpeg_bin,
            "out_width": out_width,
            "out_height": out_height,
            "model_id": detector_config.model_id,
            "device": detector_config.device,
            "confidence": detector_config.confidence,
            "batch_size": detector_config.batch_size,
            "decode_width": detector_config.decode_width,
            "min_box_height_ratio": detector_config.min_box_height_ratio,
        }
        for i, (span_start, span_end) in enumerate(spans)
    ]

    context = mp.get_context("spawn")
    with ProcessPoolExecutor(max_workers=len(tasks), mp_context=context) as pool:
        results = list(pool.map(run_chunk, tasks))  # map preserves chunk order

    counts: list[int] = []
    for result in results:
        expected = result["end_index"] - result["start_index"]
        chunk_counts = list(result["counts"])
        if len(chunk_counts) < expected:
            short_by = expected - len(chunk_counts)
            pad_value = chunk_counts[-1] if chunk_counts else 0
            log(f"chunk {result['chunk_index']} decoded {short_by} sample(s) short — padding with {pad_value}.")
            stats.padded_samples += short_by
            chunk_counts.extend([pad_value] * short_by)
        counts.extend(chunk_counts)
        stats.decode_seconds += result["decode_seconds"]
        stats.inference_seconds += result["inference_seconds"]
        stats.oom_batch_reductions += result["oom_batch_reductions"]
        stats.rejected_small_boxes += int(result.get("rejected_small_boxes") or 0)
        stats.cpu_fallback = stats.cpu_fallback or result["cpu_fallback"]

    device = ", ".join(sorted({r["device"] for r in results}))
    model = results[0]["model"]
    return counts, device, model


def detect_counts_single(
    *,
    video_path: Path,
    ffmpeg_bin: str,
    sample_fps: float,
    out_width: int,
    out_height: int,
    expected_samples: int,
    detector_config: DetectorConfig,
    stats: DetectionStats,
) -> tuple[list[int], str, str]:
    """Single-process decode + detect (decodes to EOF — no padding needed)."""
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
            sample_fps=sample_fps,
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
                    # "Progress: N%" is the wire format every long-running step
                    # in this pipeline reports in (WhisperX prints it natively;
                    # app/pipeline/progress_tracker.py parses it). Emitting it
                    # here is what turns the session card's fixed midpoint into
                    # a moving bar during a detection run that can last many
                    # minutes without any other sign of life.
                    log(f"Analysed {len(counts)} frames — Progress: {done_pct:.1f}%")
        if batch:
            inference_started = time.perf_counter()
            counts.extend(counter.count_people(batch, stats))
            inference_seconds += time.perf_counter() - inference_started
    finally:
        counter.close()

    stats.decode_seconds = time.perf_counter() - decode_started - inference_seconds
    stats.inference_seconds = inference_seconds
    return counts, counter.device, counter.resolved_model_id


# ---------------------------------------------------------------------------
# Self-check (pure math — no model, no video)
# ---------------------------------------------------------------------------


def self_check() -> None:
    # Chunk split: contiguous cover, never an empty span.
    for total, workers in [(10, 2), (11, 3), (1, 4), (0, 2), (100, 1), (7, 7), (5, 100)]:
        spans = split_sample_indices(total, workers)
        assert all(a < b for a, b in spans), f"empty span in {spans}"
        flat = [i for a, b in spans for i in range(a, b)]
        assert flat == list(range(total)), f"split of {total}/{workers} not a contiguous cover: {spans}"
    assert split_sample_indices(0, 2) == []

    # Tolerant segmenter: 100 idle, 100 active with 8 scattered dropouts (within
    # the 10-sample tolerance), 100 idle -> exactly one session, anchored at the
    # first active sample and cut at the first idle sample. Dropouts sit clear of
    # the session end: a dropout within ~tolerance samples of the true end would
    # legitimately anchor the cut earlier (by design — first bad sample wins).
    counts = [0] * 100 + [2] * 100 + [0] * 100
    for miss in (110, 125, 140, 155, 160, 170, 175, 180):
        counts[miss] = 1  # detector flicker inside the session
    kwargs = dict(
        min_people=2,
        sample_dt=1.0,
        start_window_samples=50,
        start_tolerance_samples=10,
        end_window_samples=50,
        end_tolerance_samples=10,
        start_offset_seconds=0.0,
        end_offset_seconds=0.0,
        min_clip_seconds=0.5,
    )
    ranges, _transitions = build_tolerant_session_ranges(counts, 300.0, **kwargs)
    assert len(ranges) == 1, f"expected 1 session, got {ranges}"
    assert ranges[0]["start"] == 100.0 and ranges[0]["end"] == 200.0, ranges
    assert ranges[0]["student_index"] == 1

    # >10 disagreements in every window -> no session confirmed.
    noisy = [2 if i % 3 == 0 else 0 for i in range(300)]  # 2/3 of samples idle
    ranges_noisy, _ = build_tolerant_session_ranges(noisy, 300.0, **kwargs)
    assert ranges_noisy == [], ranges_noisy

    # Timeline partition: gapless ordered cover; sessions match clip_ranges.
    segments = build_timeline_segments(counts, ranges, 300.0, 1.0)
    assert segments[0]["start"] == 0.0 and segments[-1]["end"] == 300.0
    for left, right in zip(segments, segments[1:]):
        assert left["end"] == right["start"], f"partition gap: {left} -> {right}"
    sessions = [s for s in segments if s["kind"] == SESSION_KIND]
    assert [(s["start"], s["end"]) for s in sessions] == [(r["start"], r["end"]) for r in ranges]
    assert all(s["kind"] == INTERMISSION_KIND for s in segments if s not in sessions)
    assert segments[0]["person_count"] == 0  # empty room before the session

    # No sessions at all -> one whole-video intermission.
    empty_segments = build_timeline_segments([0] * 60, [], 60.0, 1.0)
    assert len(empty_segments) == 1
    assert empty_segments[0] == {"start": 0.0, "end": 60.0, "kind": INTERMISSION_KIND, "person_count": 0}

    # Height gate: a full body (0.8 of frame) plus a limb at the edge (0.2)
    # counts as ONE person once the gate is on, and as two when it is off.
    body = (10.0, 20.0, 90.0, 180.0)  # 160px tall in a 200px frame -> 0.8
    limb = (180.0, 150.0, 199.0, 190.0)  # 40px -> 0.2
    assert count_valid_people([body, limb], 200.0, 0.0) == (2, 0)
    assert count_valid_people([body, limb], 200.0, 0.4) == (1, 1)
    assert count_valid_people([body, limb], 200.0, 0.9) == (0, 2)

    # Session-duration guard: a 60s confirmed range is dropped by a 120s floor
    # while a 300s one survives, and the survivor is still Student 1.
    short_then_long = [0] * 60 + [2] * 60 + [0] * 120 + [2] * 300 + [0] * 120
    guarded_kwargs = {**kwargs, "min_session_seconds": 120.0}
    guarded, _ = build_tolerant_session_ranges(short_then_long, 660.0, **guarded_kwargs)
    assert len(guarded) == 1, guarded
    assert guarded[0]["student_index"] == 1 and guarded[0]["end"] - guarded[0]["start"] >= 120.0
    unguarded, _ = build_tolerant_session_ranges(short_then_long, 660.0, **kwargs)
    assert len(unguarded) == 2, unguarded

    # Presets resolve to the numbers the docstring promises, and an unknown id
    # degrades to the default rather than killing a queued run.
    assert person_presets.resolve("solo").min_people == 1
    assert person_presets.resolve("pair").min_box_height_ratio == 0.0
    assert person_presets.resolve("nonsense").id == person_presets.DEFAULT_PRESET

    print("self-check OK")


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
    parser.add_argument("--video", default="", help="Path to the source video file")
    parser.add_argument(
        "--video-duration",
        type=float,
        default=0.0,
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
        "--workers",
        type=int,
        default=read_int_env("HUMAN_SEGMENTS_WORKERS", 1),
        help=(
            "Parallel detector processes over time-chunks (default 1). Each worker "
            "loads its own model copy — leave at 1 on a single small GPU."
        ),
    )
    parser.add_argument(
        "--preset",
        default=os.getenv("HUMAN_SEGMENTS_PRESET") or person_presets.DEFAULT_PRESET,
        choices=list(person_presets.PRESET_IDS),
        help=(
            "Occupancy preset supplying min-people / min-box-height-ratio / "
            f"min-session-seconds (default {person_presets.DEFAULT_PRESET}). "
            "Any of those flags given explicitly wins over the preset."
        ),
    )
    # The three preset-backed knobs default to None so "not given" is
    # distinguishable from "given the same value the preset holds" — that is
    # what lets an explicit flag override a preset without the preset having to
    # know which flags exist.
    parser.add_argument(
        "--min-people",
        type=int,
        default=None,
        help="People required on screen for a session to count as active (preset default)",
    )
    parser.add_argument(
        "--min-box-height-ratio",
        type=float,
        default=None,
        help=(
            "Smallest detection height, as a fraction of frame height, that counts "
            "as a person (preset default; 0 disables). Rejects limbs and passers-by "
            "intruding at the frame edge, which a confidence threshold cannot."
        ),
    )
    parser.add_argument(
        "--min-session-seconds",
        type=float,
        default=None,
        help="Discard confirmed sessions shorter than this (preset default; 0 disables)",
    )
    parser.add_argument(
        "--start-after-seconds",
        type=float,
        default=read_float_env("HUMAN_SEGMENTS_START_AFTER_SECONDS", 50.0),
        help=(
            "Confirmation window (seconds, at --sample-fps) that must be mostly "
            "person-positive to open a clip (default 50)"
        ),
    )
    parser.add_argument(
        "--end-after-seconds",
        type=float,
        default=read_float_env("HUMAN_SEGMENTS_END_AFTER_SECONDS", 50.0),
        help=(
            "Confirmation window (seconds, at --sample-fps) that must be mostly "
            "person-negative to close a clip (default 50)"
        ),
    )
    parser.add_argument(
        "--flicker-tolerance-seconds",
        type=float,
        default=read_float_env("HUMAN_SEGMENTS_FLICKER_TOLERANCE_SECONDS", 10.0),
        help="Max disagreeing samples tolerated inside a confirmation window (default 10)",
    )
    parser.add_argument(
        "--median-window",
        type=int,
        default=read_int_env("HUMAN_SEGMENTS_MEDIAN_WINDOW", 3),
        help="Unused (superseded by the tolerant N-of-M segmenter); accepted for CLI compatibility",
    )
    parser.add_argument("--start-offset", type=float, default=0.0, help="Seconds to pad before each clip start")
    parser.add_argument("--end-offset", type=float, default=2.0, help="Seconds to pad after each clip end")
    parser.add_argument("--min-clip-seconds", type=float, default=0.5, help="Minimum clip duration to keep")
    parser.add_argument(
        "--confidence",
        type=float,
        default=read_float_env("HUMAN_SEGMENTS_CONFIDENCE", 0.7),
        help="Detection score threshold for counting a person (default 0.7)",
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
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="Run the pure-math asserts (chunk split, segmenter, partition) and exit",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.self_check:
        self_check()
        return 0
    if not args.video:
        raise ValueError("--video is required.")
    video_path = Path(args.video).expanduser().resolve()
    if not video_path.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")
    video_duration = float(args.video_duration)
    if video_duration <= 0:
        raise ValueError("--video-duration must be a positive number of seconds.")

    # Precedence: explicit flag > environment > preset. The environment layer
    # keeps the documented HUMAN_SEGMENTS_* overrides working for deployments
    # that tuned them before presets existed.
    preset = person_presets.resolve(
        args.preset,
        min_people=(
            args.min_people
            if args.min_people is not None
            else (read_int_env("HUMAN_SEGMENTS_MIN_PEOPLE", 0) or None)
        ),
        min_box_height_ratio=(
            args.min_box_height_ratio
            if args.min_box_height_ratio is not None
            else _optional_float_env("HUMAN_SEGMENTS_MIN_BOX_HEIGHT_RATIO")
        ),
        min_session_seconds=(
            args.min_session_seconds
            if args.min_session_seconds is not None
            else _optional_float_env("HUMAN_SEGMENTS_MIN_SESSION_SECONDS")
        ),
    )
    log(
        f"Occupancy preset '{preset.id}': min_people={preset.min_people}, "
        f"min_box_height_ratio={preset.min_box_height_ratio:g}, "
        f"min_session_seconds={preset.min_session_seconds:g}."
    )

    segmenter_config = SegmenterConfig(
        sample_fps=max(0.1, float(args.sample_fps)),
        min_people=preset.min_people,
        start_after_seconds=max(0.0, float(args.start_after_seconds)),
        end_after_seconds=max(0.0, float(args.end_after_seconds)),
        flicker_tolerance_seconds=max(0.0, float(args.flicker_tolerance_seconds)),
        start_offset_seconds=max(0.0, float(args.start_offset)),
        end_offset_seconds=max(0.0, float(args.end_offset)),
        min_clip_seconds=max(0.0, float(args.min_clip_seconds)),
        min_session_seconds=preset.min_session_seconds,
    )
    detector_config = DetectorConfig(
        model_id=str(args.model),
        device=str(args.device),
        confidence=min(0.99, max(0.05, float(args.confidence))),
        batch_size=max(1, int(args.batch_size)),
        decode_width=max(160, int(args.decode_width)),
        min_box_height_ratio=preset.min_box_height_ratio,
    )
    workers = max(1, int(args.workers))

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
        f"~{expected_samples} frames expected, {workers} worker(s))."
    )

    stats = DetectionStats(effective_batch_size=detector_config.batch_size, workers=workers)
    if workers > 1:
        counts, device, resolved_model = detect_counts_parallel(
            workers=workers,
            total_samples=expected_samples,
            video_path=video_path,
            ffmpeg_bin=ffmpeg_bin,
            sample_fps=segmenter_config.sample_fps,
            sample_dt=segmenter_config.sample_dt,
            out_width=out_width,
            out_height=out_height,
            detector_config=detector_config,
            stats=stats,
        )
    else:
        counts, device, resolved_model = detect_counts_single(
            video_path=video_path,
            ffmpeg_bin=ffmpeg_bin,
            sample_fps=segmenter_config.sample_fps,
            out_width=out_width,
            out_height=out_height,
            expected_samples=expected_samples,
            detector_config=detector_config,
            stats=stats,
        )
    stats.decode_seconds = round(stats.decode_seconds, 3)
    stats.inference_seconds = round(stats.inference_seconds, 3)
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

    clip_ranges, transitions = build_tolerant_session_ranges(
        counts,
        video_duration,
        min_people=segmenter_config.min_people,
        sample_dt=segmenter_config.sample_dt,
        start_window_samples=segmenter_config.seconds_to_samples(segmenter_config.start_after_seconds),
        start_tolerance_samples=segmenter_config.seconds_to_samples(segmenter_config.flicker_tolerance_seconds),
        end_window_samples=segmenter_config.seconds_to_samples(segmenter_config.end_after_seconds),
        end_tolerance_samples=segmenter_config.seconds_to_samples(segmenter_config.flicker_tolerance_seconds),
        start_offset_seconds=segmenter_config.start_offset_seconds,
        end_offset_seconds=segmenter_config.end_offset_seconds,
        min_clip_seconds=segmenter_config.min_clip_seconds,
        min_session_seconds=segmenter_config.min_session_seconds,
    )
    used_trigger = "person_presence"
    if not clip_ranges and args.allow_fallback_full_video:
        log("No session boundaries found — falling back to one full-video clip.")
        clip_ranges = [{"start": 0.0, "end": video_duration, "student_index": 1}]
        used_trigger = "fallback_full_video"

    timeline_segments = build_timeline_segments(
        counts, clip_ranges, video_duration, segmenter_config.sample_dt
    )
    intermission_count = sum(1 for segment in timeline_segments if segment["kind"] == INTERMISSION_KIND)
    log(
        f"Detected {len(clip_ranges)} session clip(s) and {intermission_count} "
        f"intermission(s) via {used_trigger}."
    )

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
        "model": resolved_model,
        "device": device,
        "confidence": detector_config.confidence,
        "sample_fps": segmenter_config.sample_fps,
        "sampled_frames": stats.sampled_frames,
        "preset": preset.id,
        "min_people": segmenter_config.min_people,
        "min_box_height_ratio": detector_config.min_box_height_ratio,
        "min_session_seconds": segmenter_config.min_session_seconds,
        "start_after_seconds": segmenter_config.start_after_seconds,
        "end_after_seconds": segmenter_config.end_after_seconds,
        "flicker_tolerance_seconds": segmenter_config.flicker_tolerance_seconds,
        "start_offset": segmenter_config.start_offset_seconds,
        "end_offset": segmenter_config.end_offset_seconds,
        "min_clip_seconds": segmenter_config.min_clip_seconds,
        "student_count": len(clip_ranges),
        "clip_ranges": clip_ranges,
        "timeline_segments": timeline_segments,
        "transitions": transitions,
        "person_count_histogram": stats.person_count_histogram,
        "debug": {
            "decode_seconds": stats.decode_seconds,
            "inference_seconds": stats.inference_seconds,
            "batch_size": detector_config.batch_size,
            "workers": stats.workers,
            "padded_samples": stats.padded_samples,
            "rejected_small_boxes": stats.rejected_small_boxes,
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
