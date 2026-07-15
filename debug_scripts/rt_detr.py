#!/usr/bin/env python3
"""Debug/visualisation harness for the production RT-DETR human-presence detector.

Mirrors ``scripts/detect_human_segments.py`` exactly for the detection +
segmentation maths (it imports and reuses that module), but instead of only
emitting clip ranges as JSON it ALSO:

  * draws the person bounding boxes on every sampled frame and writes each
    annotated frame as a PNG (so you can eyeball what the detector saw), and
  * writes an .xlsx report summarising how many student clips were detected,
    from/to which sampled frame, plus a per-frame person-count log.

Run (uses the project's .venv which has torch+cuda+transformers):

    OSCE-AI-FYP/.venv/Scripts/python.exe debug_scripts/rt_detr.py \
        --video "C:/Users/.../Common Cold_Session 1.mp4"

Defaults point at the requested output locations:
    annotations -> C:/Users/yeeye/Downloads/ra test output/annotations_2
    report      -> C:/Users/yeeye/Downloads/ra test output/rt_detr_report_2.xlsx

This is a debug script: it does NOT touch production code, only imports it.

Segmentation tuning (this file only — production ``detect_human_segments.py``
is untouched until this is validated against real footage):

  * Per-box confidence gate raised to 0.7 (``--confidence``) — a detection
    below that score is not counted as a person at all, cutting false
    positives from partial/occluded bodies.
  * Session start/end no longer use the production module's morphological
    open+close (``dhs.build_session_ranges``); that approach flips ANY
    interior run shorter than the flicker-tolerance regardless of value, so
    it can just as easily erase a short *genuine* active stretch as it
    absorbs a short misclassification run once the tolerance is large. See
    ``build_tolerant_session_ranges`` below for the replacement: a
    N-of-M ("at most K bad samples in a window of W") confirmation, which is
    the standard debounce technique for noisy boolean sensors and is what
    the request actually described ("40 good + 10 bad in 50 frames = start
    the clip"). Both boundaries reuse the identical mechanism
    (``--start-after-seconds``/``--flicker-tolerance-seconds`` for the start
    window, ``--end-after-seconds``/``--flicker-tolerance-seconds`` for the
    end window), so start confirmation and clip cut-off behave the same way.
  * The confirmed clip is anchored to the FIRST sample that individually
    crossed the threshold, not the sample where confirmation completed.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Import the production detector/segmenter as-is (single source of truth).
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))
import detect_human_segments as dhs  # noqa: E402

DEFAULT_VIDEO = r"C:\Users\yeeye\Downloads\RA Dataset\video\Common Cold_Session 1.mp4"
DEFAULT_OUT_ROOT = r"C:\Users\yeeye\Downloads\ra test output"


# ---------------------------------------------------------------------------
# Tolerant N-of-M session segmentation (debug-only variant).
#
# Confirms a state transition once a fixed-length sample window has AT MOST
# `tolerance` samples that disagree with the target state ("at most 10 bad
# samples out of a 50-sample window"). This is symmetric: the exact same
# check confirms both session start and session end, so a clip's cut-off
# tolerates model bottleneck the same way its start does.
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
) -> tuple[list[dict], list[dict]]:
    """N-of-M hysteresis over per-sample person counts.

    Anchors each confirmed clip to the FIRST sample that individually
    crossed ``min_people`` (or dropped below it), not the sample where the
    confirmation window completed — mirrors "take the first frame identified
    as 2 people and start the clip from there".
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

    # Padding + clamping + minimum-duration filtering — same semantics as
    # dhs.build_session_ranges (offset padding, overlap guard, min duration).
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
    for i, clip_range in enumerate(padded, start=1):
        clip_range["student_index"] = i
    return padded, transitions


# ---------------------------------------------------------------------------
# Person detection WITH boxes.
#
# Production PersonCounter only returns per-frame counts. We reuse its loaded
# model/processor but post-process into (box, score) lists so we can draw them.
# OOM-halving mirrors PersonCounter.count_people so a long run can't die on one
# fat batch.
# ---------------------------------------------------------------------------


def detect_person_boxes(counter: "dhs.PersonCounter", frames: list) -> list[list[tuple]]:
    import torch

    if not frames:
        return []
    try:
        inputs = counter.processor(images=frames, return_tensors="pt")
        pixel_values = inputs["pixel_values"].to(counter.device)
        if counter.use_fp16:
            pixel_values = pixel_values.half()
        with torch.inference_mode():
            outputs = counter.model(pixel_values=pixel_values)
        target_sizes = torch.tensor([f.shape[:2] for f in frames])
        results = counter.processor.post_process_object_detection(
            outputs, threshold=counter.config.confidence, target_sizes=target_sizes
        )
    except torch.cuda.OutOfMemoryError:
        torch.cuda.empty_cache()
        if len(frames) > 1:
            half = len(frames) // 2
            dhs.log(f"CUDA OOM — splitting batch {len(frames)} -> {half}+{len(frames) - half}.")
            return detect_person_boxes(counter, frames[:half]) + detect_person_boxes(counter, frames[half:])
        dhs.log("CUDA OOM on a single frame — moving detector to CPU for the rest of the run.")
        counter.device = "cpu"
        counter.use_fp16 = False
        counter.model = counter.model.float().to("cpu")
        torch.cuda.empty_cache()
        return detect_person_boxes(counter, frames)

    per_frame: list[list[tuple]] = []
    for result in results:
        boxes = []
        for label, score, box in zip(
            result["labels"].tolist(), result["scores"].tolist(), result["boxes"].tolist()
        ):
            if int(label) in counter.person_label_ids:
                boxes.append((box, float(score)))
        per_frame.append(boxes)
    return per_frame


def annotate_and_save(frame, boxes: list[tuple], out_path: Path) -> None:
    from PIL import Image, ImageDraw

    img = Image.fromarray(frame)  # HxWx3 uint8 RGB (fromarray copies the read-only buffer)
    draw = ImageDraw.Draw(img)
    for (x0, y0, x1, y1), score in boxes:
        draw.rectangle([x0, y0, x1, y1], outline=(255, 40, 40), width=2)
        label = f"person {score:.2f}"
        ty = max(0, y0 - 11)
        # cheap readable label: filled backing box under the text
        draw.rectangle([x0, ty, x0 + 7 * len(label), ty + 11], fill=(255, 40, 40))
        draw.text((x0 + 1, ty), label, fill=(255, 255, 255))
    draw.text((3, 3), f"{len(boxes)} person(s)", fill=(255, 255, 0))
    img.save(out_path)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _fmt_ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def write_report(
    report_path: Path,
    *,
    video_path: Path,
    video_duration: float,
    seg_cfg: "dhs.SegmenterConfig",
    resolved_model: str,
    device: str,
    confidence: float,
    resolution: str,
    counts: list[int],
    frame_rows: list[dict],
    clip_ranges: list[dict],
    transitions: list[dict],
    histogram: dict[str, int],
    decode_seconds: float,
    inference_seconds: float,
    annotate_seconds: float,
    annotations_dir: Path,
) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    dt = seg_cfg.sample_dt
    bold = Font(bold=True)

    def autofit(ws, max_width: int = 60) -> None:
        for col in ws.columns:
            width = max((len(str(c.value)) for c in col if c.value is not None), default=0)
            ws.column_dimensions[get_column_letter(col[0].column)].width = min(max_width, width + 2)

    # --- Summary ---
    ws = wb.active
    ws.title = "Summary"
    summary = [
        ("OSCE RT-DETR Human-Presence Detection — Report", ""),
        ("Generated", time.strftime("%Y-%m-%d %H:%M:%S")),
        ("Video file", str(video_path)),
        ("Video duration", f"{video_duration:.1f} s ({_fmt_ts(video_duration)})"),
        ("Decode resolution", resolution),
        ("Model", resolved_model),
        ("Device", device),
        ("Confidence threshold", confidence),
        ("Sample rate (fps)", seg_cfg.sample_fps),
        ("Min people for 'active'", seg_cfg.min_people),
        ("Start confirm (s)", seg_cfg.start_after_seconds),
        ("End confirm (s)", seg_cfg.end_after_seconds),
        ("Flicker tolerance (s)", seg_cfg.flicker_tolerance_seconds),
        ("Median window", seg_cfg.median_window),
        ("Start/End padding (s)", f"{seg_cfg.start_offset_seconds} / {seg_cfg.end_offset_seconds}"),
        ("", ""),
        ("Sampled frames analysed", len(counts)),
        ("Annotated frames written", len(frame_rows)),
        ("STUDENT CLIPS DETECTED", len(clip_ranges)),
        ("", ""),
        ("Decode time (s)", round(decode_seconds, 1)),
        ("Inference time (s)", round(inference_seconds, 1)),
        ("Annotate+save time (s)", round(annotate_seconds, 1)),
        ("Annotations folder", str(annotations_dir)),
    ]
    for row in summary:
        ws.append(list(row))
    ws["A1"].font = Font(bold=True, size=14)
    for r in range(2, len(summary) + 2):
        ws.cell(row=r, column=1).font = bold
    autofit(ws)

    # --- Clips: the headline answer (how many, from which frame) ---
    ws = wb.create_sheet("Clips")
    headers = [
        "student_index", "start_s", "end_s", "duration_s",
        "start_frame", "end_frame", "n_frames", "start_tc", "end_tc",
    ]
    ws.append(headers)
    for clip in clip_ranges:
        start_f = round(clip["start"] / dt)
        end_f = round(clip["end"] / dt)
        ws.append([
            clip.get("student_index"),
            round(clip["start"], 2),
            round(clip["end"], 2),
            round(clip["end"] - clip["start"], 2),
            start_f,
            end_f,
            end_f - start_f + 1,
            _fmt_ts(clip["start"]),
            _fmt_ts(clip["end"]),
        ])
    for c in ws[1]:
        c.font = bold
    autofit(ws)

    # --- Frames: per-sample log ---
    ws = wb.create_sheet("Frames")
    ws.append(["frame_index", "time_s", "timecode", "person_count", "in_session", "clip_index", "annotated_file"])
    for row in frame_rows:
        ws.append([
            row["frame_index"], round(row["time_s"], 2), _fmt_ts(row["time_s"]),
            row["person_count"], row["in_session"], row["clip_index"], row["file"],
        ])
    for c in ws[1]:
        c.font = bold
    ws.freeze_panes = "A2"
    autofit(ws, max_width=45)

    # --- Transitions & histogram ---
    ws = wb.create_sheet("Transitions")
    ws.append(["at_s", "timecode", "event", "run_samples"])
    for t in transitions:
        ws.append([t.get("at"), _fmt_ts(t.get("at", 0.0)), t.get("event"), t.get("run_samples", "")])
    for c in ws[1]:
        c.font = bold
    ws.append([])
    ws.append(["Person-count histogram"])
    ws.append(["people_in_frame", "num_frames"])
    for k in sorted(histogram, key=lambda x: int(x)):
        ws.append([int(k), histogram[k]])
    autofit(ws)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(report_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="RT-DETR human-detection debug: annotate frames + Excel report.")
    p.add_argument("--video", default=DEFAULT_VIDEO)
    p.add_argument("--annotations-dir", default=str(Path(DEFAULT_OUT_ROOT) / "annotations_2"))
    p.add_argument("--report", default=str(Path(DEFAULT_OUT_ROOT) / "rt_detr_report_2.xlsx"))
    p.add_argument("--sample-fps", type=float, default=1.0)
    p.add_argument("--min-people", type=int, default=2)
    p.add_argument(
        "--start-after-seconds", type=float, default=50.0,
        help="Confirmation window (samples, at sample-fps) that must be mostly person-positive to open a clip",
    )
    p.add_argument(
        "--end-after-seconds", type=float, default=50.0,
        help="Confirmation window (samples, at sample-fps) that must be mostly person-negative to close a clip",
    )
    p.add_argument(
        "--flicker-tolerance-seconds", type=float, default=10.0,
        help="Max disagreeing samples tolerated inside a confirmation window (model-bottleneck tolerance)",
    )
    p.add_argument("--median-window", type=int, default=3, help="Unused by the tolerant segmenter; kept for CLI parity")
    p.add_argument("--start-offset", type=float, default=0.0)
    p.add_argument("--end-offset", type=float, default=2.0)
    p.add_argument("--min-clip-seconds", type=float, default=0.5)
    p.add_argument(
        "--confidence", type=float, default=0.7,
        help="Per-box detection score required for a box to count as a person (default 0.7)",
    )
    p.add_argument("--model", default=dhs.DEFAULT_MODEL)
    p.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    p.add_argument("--batch-size", type=int, default=8)
    p.add_argument("--decode-width", type=int, default=640)
    p.add_argument("--max-frames", type=int, default=0, help="Cap sampled frames (0 = all) — for quick tests")
    p.add_argument("--no-annotate", action="store_true", help="Skip writing PNGs (counts + report only)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    video_path = Path(args.video).expanduser().resolve()
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")

    annotations_dir = Path(args.annotations_dir).expanduser()
    annotations_dir.mkdir(parents=True, exist_ok=True)
    report_path = Path(args.report).expanduser()

    seg_cfg = dhs.SegmenterConfig(
        sample_fps=max(0.1, args.sample_fps),
        min_people=max(1, args.min_people),
        start_after_seconds=max(0.0, args.start_after_seconds),
        end_after_seconds=max(0.0, args.end_after_seconds),
        flicker_tolerance_seconds=max(0.0, args.flicker_tolerance_seconds),
        median_window=max(1, args.median_window),
        start_offset_seconds=max(0.0, args.start_offset),
        end_offset_seconds=max(0.0, args.end_offset),
        min_clip_seconds=max(0.0, args.min_clip_seconds),
    )
    det_cfg = dhs.DetectorConfig(
        model_id=args.model,
        device=args.device,
        confidence=min(0.99, max(0.05, args.confidence)),
        batch_size=max(1, args.batch_size),
        decode_width=max(160, args.decode_width),
    )

    ffmpeg_bin = dhs.resolve_binary("FFMPEG_BIN", "ffmpeg")
    ffprobe_bin = dhs.resolve_binary("FFPROBE_BIN", "ffprobe")
    src_w, src_h = dhs.probe_video_dimensions(ffprobe_bin, video_path)
    out_w = min(det_cfg.decode_width, src_w)
    out_h = max(2, round(src_h * out_w / src_w / 2) * 2)
    out_w = max(2, out_w // 2 * 2)

    # Duration from ffprobe (production takes it from the caller; here we probe).
    import json
    import subprocess

    probe = subprocess.run(
        [ffprobe_bin, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video_path)],
        capture_output=True, text=True, check=True,
    )
    video_duration = float(json.loads(probe.stdout)["format"]["duration"])

    dhs.log(
        f"Sampling {video_path.name} at {seg_cfg.sample_fps} fps "
        f"({src_w}x{src_h} -> {out_w}x{out_h}); duration {_fmt_ts(video_duration)}."
    )

    counter = dhs.PersonCounter(det_cfg)
    counter.load()

    counts: list[int] = []
    all_boxes: list[list[tuple]] = []
    batch: list = []
    batch_frames_for_draw: list = []
    frame_index = 0
    decode_started = time.perf_counter()
    inference_seconds = 0.0
    annotate_seconds = 0.0

    def flush(batch, draw_frames, start_idx):
        nonlocal inference_seconds, annotate_seconds
        t0 = time.perf_counter()
        boxes = detect_person_boxes(counter, batch)
        inference_seconds += time.perf_counter() - t0
        for j, fb in enumerate(boxes):
            counts.append(len(fb))
            all_boxes.append(fb)
        if not args.no_annotate:
            t1 = time.perf_counter()
            for j, (frm, fb) in enumerate(zip(draw_frames, boxes)):
                idx = start_idx + j
                t_s = idx * seg_cfg.sample_dt
                out = annotations_dir / f"frame_{idx:06d}_t{int(t_s):06d}s.png"
                annotate_and_save(frm, fb, out)
            annotate_seconds += time.perf_counter() - t1

    try:
        for frame in dhs.iter_sampled_frames(
            ffmpeg_bin, video_path, sample_fps=seg_cfg.sample_fps, out_width=out_w, out_height=out_h
        ):
            batch.append(frame)
            batch_frames_for_draw.append(frame)
            if len(batch) >= det_cfg.batch_size:
                flush(batch, batch_frames_for_draw, len(counts))
                batch, batch_frames_for_draw = [], []
                if len(counts) % 120 < det_cfg.batch_size:
                    pct = 100.0 * len(counts) / max(1, int(video_duration * seg_cfg.sample_fps))
                    dhs.log(f"Analysed {len(counts)} frames (~{pct:.0f}%).")
            frame_index += 1
            if args.max_frames and frame_index >= args.max_frames:
                break
        if batch:
            flush(batch, batch_frames_for_draw, len(counts))
    finally:
        counter.close()

    decode_seconds = time.perf_counter() - decode_started - inference_seconds - annotate_seconds

    if not counts:
        raise RuntimeError("No frames decoded — is the video readable?")

    histogram: dict[str, int] = {}
    for c in counts:
        histogram[str(c)] = histogram.get(str(c), 0) + 1
    dhs.log(f"Person-count histogram: {histogram}")

    clip_ranges, transitions = build_tolerant_session_ranges(
        counts,
        video_duration,
        min_people=seg_cfg.min_people,
        sample_dt=seg_cfg.sample_dt,
        start_window_samples=seg_cfg.seconds_to_samples(seg_cfg.start_after_seconds),
        start_tolerance_samples=seg_cfg.seconds_to_samples(seg_cfg.flicker_tolerance_seconds),
        end_window_samples=seg_cfg.seconds_to_samples(seg_cfg.end_after_seconds),
        end_tolerance_samples=seg_cfg.seconds_to_samples(seg_cfg.flicker_tolerance_seconds),
        start_offset_seconds=seg_cfg.start_offset_seconds,
        end_offset_seconds=seg_cfg.end_offset_seconds,
        min_clip_seconds=seg_cfg.min_clip_seconds,
    )
    dhs.log(f"Detected {len(clip_ranges)} student clip(s).")

    # Map each sampled frame to in-session / clip membership from the FINAL ranges.
    dt = seg_cfg.sample_dt
    frame_rows: list[dict] = []
    for idx, count in enumerate(counts):
        t_s = idx * dt
        clip_idx = ""
        in_session = False
        for clip in clip_ranges:
            if clip["start"] <= t_s <= clip["end"]:
                in_session = True
                clip_idx = clip.get("student_index", "")
                break
        fname = "" if args.no_annotate else f"frame_{idx:06d}_t{int(t_s):06d}s.png"
        frame_rows.append({
            "frame_index": idx, "time_s": t_s, "person_count": count,
            "in_session": in_session, "clip_index": clip_idx, "file": fname,
        })

    write_report(
        report_path,
        video_path=video_path,
        video_duration=video_duration,
        seg_cfg=seg_cfg,
        resolved_model=counter.resolved_model_id,
        device=counter.device,
        confidence=det_cfg.confidence,
        resolution=f"{out_w}x{out_h}",
        counts=counts,
        frame_rows=frame_rows,
        clip_ranges=clip_ranges,
        transitions=transitions,
        histogram=histogram,
        decode_seconds=decode_seconds,
        inference_seconds=inference_seconds,
        annotate_seconds=annotate_seconds,
        annotations_dir=annotations_dir,
    )

    # Console summary ("show" the results).
    print("\n" + "=" * 60)
    print(f"  RT-DETR human detection — {video_path.name}")
    print("=" * 60)
    print(f"  Sampled frames : {len(counts)}")
    print(f"  Annotated PNGs : {'(skipped)' if args.no_annotate else len(frame_rows)} -> {annotations_dir}")
    print(f"  Clips detected : {len(clip_ranges)}")
    for clip in clip_ranges:
        sf, ef = round(clip["start"] / dt), round(clip["end"] / dt)
        print(
            f"    #{clip['student_index']:>2}  {_fmt_ts(clip['start'])}–{_fmt_ts(clip['end'])}  "
            f"(frames {sf}–{ef}, {clip['end'] - clip['start']:.0f}s)"
        )
    print(f"  Report         : {report_path}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
