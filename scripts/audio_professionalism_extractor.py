#!/usr/bin/env python3
from __future__ import annotations

import argparse
from bisect import bisect_left
import json
import math
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from env_loader import load_env_file
from scorer_inputs import required_file, run_main, session_id_from

ROOT_DIR = Path(__file__).resolve().parents[1]
load_env_file(ROOT_DIR)
STORAGE_DIR = ROOT_DIR / "storage"
OUTPUT_DIR = STORAGE_DIR / "output" / "audio_professionalism"

FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")

MERGE_GAP_SECONDS = 0.2
MAX_FILTER_SEGMENTS = 240
LONG_PAUSE_SECONDS = 2.0
MIN_OVERLAP_SECONDS = 0.1

FILLER_PATTERNS = [
    r"\b(u+hm+|um+|uh+|erm|er|ah+)\b",
    r"\b(you know|kind of|sort of)\b",
    r"\b(like)\b",
]

NOT_OBSERVABLE_FROM_AUDIO = [
    "Eye contact",
    "Head nods",
    "Posture",
    "Body language",
    "Privacy",
    "Physical distance",
    "Absence of barriers",
    "Visual aids",
]


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(text, encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract audio professionalism metrics from OSCE audio + WhisperX transcript, "
            "producing structured JSON for communications rubric support."
        )
    )
    parser.add_argument("--session-id", help="Session ID (defaults to the audio file's stem)")
    parser.add_argument("--audio", required=True, help="Extracted session audio (MP3/WAV). Required.")
    parser.add_argument("--transcript", required=True, help="Normalised transcript JSON. Required.")
    parser.add_argument("--output", help="Output JSON path")
    parser.add_argument("--student-speaker", help="Override student speaker label (e.g., SPEAKER_03)")
    return parser.parse_args()


def infer_speaker(segment: dict[str, Any]) -> str:
    speaker = str(segment.get("speaker", "")).strip()
    if speaker:
        return speaker

    words = segment.get("words")
    if isinstance(words, list):
        for word in words:
            candidate = str(word.get("speaker", "")).strip()
            if candidate:
                return candidate

    return "SPEAKER_UNKNOWN"


def load_transcript_segments(transcript_path: Path) -> list[dict[str, Any]]:
    raw = transcript_path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []

    segments_raw = payload.get("segments") if isinstance(payload, dict) else None
    if not isinstance(segments_raw, list):
        return []

    segments: list[dict[str, Any]] = []
    for item in segments_raw:
        if not isinstance(item, dict):
            continue

        start = float(item.get("start", 0) or 0)
        end = float(item.get("end", start) or start)
        if end <= start:
            continue

        segments.append(
            {
                "start": start,
                "end": end,
                "speaker": infer_speaker(item),
                "text": str(item.get("text", "")).strip(),
            }
        )

    return segments


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def count_words(text: str) -> int:
    return len(re.findall(r"[A-Za-z']+", text))


def count_fillers(text: str) -> int:
    text = str(text or "").lower()
    count = 0
    for pattern in FILLER_PATTERNS:
        count += len(re.findall(pattern, text))
    return count


def merge_segments(segments: Iterable[dict[str, Any]], max_gap: float) -> list[dict[str, Any]]:
    sorted_segments = sorted(segments, key=lambda seg: seg["start"])
    if not sorted_segments:
        return []

    merged = [dict(sorted_segments[0])]
    for seg in sorted_segments[1:]:
        current = merged[-1]
        if seg["start"] <= current["end"] + max_gap:
            current["end"] = max(current["end"], seg["end"])
            if seg.get("text"):
                current["text"] = normalize_text(f"{current.get('text', '')} {seg.get('text', '')}")
            continue

        merged.append(dict(seg))

    return merged


def select_student_speaker(segments: list[dict[str, Any]], override: str | None) -> dict[str, Any]:
    if override:
        return {
            "id": override,
            "selection": "override",
            "confidence": 1.0,
            "gap_seconds": 0.0,
        }

    totals: dict[str, float] = {}
    for seg in segments:
        speaker = seg.get("speaker", "SPEAKER_UNKNOWN")
        totals[speaker] = totals.get(speaker, 0.0) + max(0.0, seg["end"] - seg["start"])

    if not totals:
        return {
            "id": "SPEAKER_UNKNOWN",
            "selection": "fallback",
            "confidence": 0.0,
            "gap_seconds": 0.0,
        }

    ordered = sorted(totals.items(), key=lambda item: item[1], reverse=True)
    top_speaker, top_duration = ordered[0]
    second_duration = ordered[1][1] if len(ordered) > 1 else 0.0
    gap = max(0.0, top_duration - second_duration)
    total = sum(totals.values()) or 1.0
    confidence = round(min(1.0, max(0.1, gap / total)), 3)

    return {
        "id": top_speaker,
        "selection": "max_total_duration",
        "confidence": confidence,
        "gap_seconds": round(gap, 3),
    }


def build_student_audio(
    audio_path: Path,
    student_segments: list[dict[str, Any]],
    output_path: Path,
) -> None:
    if not student_segments:
        raise ValueError("No student segments available for audio extraction.")

    segments = merge_segments(student_segments, MERGE_GAP_SECONDS)
    if len(segments) > MAX_FILTER_SEGMENTS:
        for gap in (0.4, 0.8, 1.2):
            segments = merge_segments(student_segments, gap)
            if len(segments) <= MAX_FILTER_SEGMENTS:
                break

    if len(segments) > MAX_FILTER_SEGMENTS:
        segments = sorted(segments, key=lambda seg: seg["end"] - seg["start"], reverse=True)[:MAX_FILTER_SEGMENTS]
        segments = sorted(segments, key=lambda seg: seg["start"])

    trims = []
    for index, seg in enumerate(segments):
        trims.append(
            f"[0:a]atrim=start={seg['start']}:end={seg['end']},asetpts=PTS-STARTPTS[a{index}]"
        )

    concat_inputs = "".join(f"[a{index}]" for index in range(len(segments)))
    filter_complex = ";".join(trims) + f";{concat_inputs}concat=n={len(segments)}:v=0:a=1[aout]"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        FFMPEG_BIN,
        "-y",
        "-i",
        str(audio_path),
        "-filter_complex",
        filter_complex,
        "-map",
        "[aout]",
        "-ac",
        "1",
        "-ar",
        "16000",
        str(output_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            "ffmpeg failed to generate student-only audio. "
            f"stderr: {result.stderr.strip() or 'no stderr'}"
        )


def pick_feature(row: dict[str, Any], keys: Iterable[str]) -> float | None:
    for key in keys:
        if key in row and row[key] is not None:
            try:
                return float(row[key])
            except (TypeError, ValueError):
                continue
    return None


def extract_opensmile_features(audio_path: Path) -> dict[str, Any]:
    try:
        import opensmile
    except ImportError as error:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "Missing dependency 'opensmile'. Install it with `uv sync`."
        ) from error

    smile = opensmile.Smile(
        feature_set=opensmile.FeatureSet.eGeMAPSv02,
        feature_level=opensmile.FeatureLevel.Functionals,
    )
    features = smile.process_file(str(audio_path))
    if features.empty:
        raise RuntimeError("openSMILE returned no features.")

    row = features.iloc[0].to_dict()

    pitch_mean = pick_feature(row, [
        "F0semitoneFrom27.5Hz_sma3nz_mean",
        "F0semitoneFrom27.5Hz_sma3nz_amean",
    ])
    pitch_std = pick_feature(row, [
        "F0semitoneFrom27.5Hz_sma3nz_stddev",
        "F0semitoneFrom27.5Hz_sma3nz_stddevNorm",
    ])
    pitch_range = pick_feature(row, [
        "F0semitoneFrom27.5Hz_sma3nz_pctlrange0-2",
        "F0semitoneFrom27.5Hz_sma3nz_pctlrange0-1",
    ])
    loud_mean = pick_feature(row, [
        "loudness_sma3_mean",
        "loudness_sma3_amean",
    ])
    loud_std = pick_feature(row, [
        "loudness_sma3_stddev",
        "loudness_sma3_stddevNorm",
    ])
    loud_range = pick_feature(row, [
        "loudness_sma3_pctlrange0-2",
        "loudness_sma3_pctlrange0-1",
    ])
    jitter = pick_feature(row, ["jitterLocal_sma3nz_mean"])
    shimmer = pick_feature(row, ["shimmerLocaldB_sma3nz_mean"])
    hnr = pick_feature(row, ["HNRdBACF_sma3nz_mean"])

    # Persist the FULL openSMILE eGeMAPSv02 functionals row so the communication
    # scorer (nvidia_osce_communication.py) can forward every feature to the
    # Nemotron model. We coerce numpy scalars to plain Python floats and drop
    # NaN/inf so the JSON serialises cleanly.
    def coerce_feature_value(value: Any) -> Any:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if math.isnan(numeric) or math.isinf(numeric):
            return None
        return numeric

    full_feature_summary = {
        str(key): coerce_feature_value(value) for key, value in row.items()
    }

    return {
        "feature_set": "eGeMAPSv02",
        "pitch_mean_semitone": pitch_mean,
        "pitch_std_semitone": pitch_std,
        "pitch_range_semitone": pitch_range,
        "loudness_mean": loud_mean,
        "loudness_std": loud_std,
        "loudness_range": loud_range,
        "jitter_local": jitter,
        "shimmer_local_db": shimmer,
        "hnr_db": hnr,
        "feature_keys_available": sorted(row.keys()),
        "full_feature_summary": full_feature_summary,
        "opensmile_version": getattr(opensmile, "__version__", "unknown"),
    }


def compute_overlap_events(
    student_segments: list[dict[str, Any]],
    other_segments: list[dict[str, Any]],
) -> tuple[int, float, list[dict[str, Any]]]:
    overlaps: list[dict[str, Any]] = []
    i = 0
    j = 0
    total_overlap = 0.0

    student_sorted = sorted(student_segments, key=lambda seg: seg["start"])
    other_sorted = sorted(other_segments, key=lambda seg: seg["start"])

    while i < len(student_sorted) and j < len(other_sorted):
        student = student_sorted[i]
        other = other_sorted[j]

        overlap_start = max(student["start"], other["start"])
        overlap_end = min(student["end"], other["end"])
        overlap = overlap_end - overlap_start

        if overlap >= MIN_OVERLAP_SECONDS:
            total_overlap += overlap
            overlaps.append(
                {
                    "type": "overlap",
                    "start": round(overlap_start, 3),
                    "end": round(overlap_end, 3),
                    "overlap_seconds": round(overlap, 3),
                    "other_speaker": other.get("speaker", "SPEAKER_UNKNOWN"),
                }
            )

        if student["end"] <= other["end"]:
            i += 1
        else:
            j += 1

    return len(overlaps), round(total_overlap, 3), overlaps


def compute_response_gaps(
    student_segments: list[dict[str, Any]],
    patient_segments: list[dict[str, Any]],
) -> tuple[list[float], list[dict[str, Any]]]:
    student_sorted = sorted(student_segments, key=lambda seg: seg["start"])
    patient_sorted = sorted(patient_segments, key=lambda seg: seg["end"])
    if not student_sorted or not patient_sorted:
        return [], []

    student_starts = [seg["start"] for seg in student_sorted]
    gaps: list[float] = []
    evidence: list[dict[str, Any]] = []

    for patient in patient_sorted:
        end = patient["end"]
        index = bisect_left(student_starts, end)

        if index >= len(student_sorted):
            continue

        gap = student_sorted[index]["start"] - end
        if gap < 0:
            continue

        gaps.append(gap)
        if gap >= LONG_PAUSE_SECONDS:
            evidence.append(
                {
                    "type": "response_gap",
                    "start": round(end, 3),
                    "end": round(student_sorted[index]["start"], 3),
                    "gap_seconds": round(gap, 3),
                }
            )

    return gaps, evidence


def build_evidence(
    long_pause_evidence: list[dict[str, Any]],
    overlap_evidence: list[dict[str, Any]],
    response_gap_evidence: list[dict[str, Any]],
    limit: int = 12,
) -> list[dict[str, Any]]:
    combined = []
    for group in (long_pause_evidence, overlap_evidence, response_gap_evidence):
        for item in group:
            if len(combined) >= limit:
                return combined
            combined.append(item)
    return combined


def main() -> int:
    args = parse_args()
    # Inputs are handed over, never searched for (scripts/scorer_inputs.py).
    audio_path = required_file(args.audio, flag="--audio", label="Audio file")
    transcript_path = required_file(args.transcript, flag="--transcript", label="Transcript")
    session_id = session_id_from(args, audio_path)

    segments = load_transcript_segments(transcript_path)
    if not segments:
        raise RuntimeError("Transcript did not contain any usable segments.")

    student_choice = select_student_speaker(segments, args.student_speaker)
    student_speaker = student_choice["id"]

    student_segments = [seg for seg in segments if seg.get("speaker") == student_speaker]
    patient_segments = [seg for seg in segments if seg.get("speaker") != student_speaker]

    student_segments = merge_segments(student_segments, MERGE_GAP_SECONDS)
    patient_segments = merge_segments(patient_segments, MERGE_GAP_SECONDS)

    student_duration = sum(max(0.0, seg["end"] - seg["start"]) for seg in student_segments)
    total_duration = sum(max(0.0, seg["end"] - seg["start"]) for seg in segments)
    total_duration = total_duration or 1.0

    student_text = normalize_text(" ".join(seg.get("text", "") for seg in student_segments))
    student_word_count = count_words(student_text)
    filler_count = count_fillers(student_text)

    student_wpm = (student_word_count / (student_duration / 60.0)) if student_duration > 0 else 0.0
    filler_per_minute = (filler_count / (student_duration / 60.0)) if student_duration > 0 else 0.0

    long_pause_evidence: list[dict[str, Any]] = []
    long_pause_count = 0
    student_sorted = sorted(student_segments, key=lambda seg: seg["start"])
    for prev_seg, next_seg in zip(student_sorted, student_sorted[1:]):
        gap = next_seg["start"] - prev_seg["end"]
        if gap >= LONG_PAUSE_SECONDS:
            long_pause_count += 1
            long_pause_evidence.append(
                {
                    "type": "long_pause",
                    "start": round(prev_seg["end"], 3),
                    "end": round(next_seg["start"], 3),
                    "gap_seconds": round(gap, 3),
                }
            )

    overlap_count, overlap_seconds, overlap_evidence = compute_overlap_events(student_segments, patient_segments)

    response_gaps, response_gap_evidence = compute_response_gaps(student_segments, patient_segments)
    mean_response_gap = round(sum(response_gaps) / len(response_gaps), 3) if response_gaps else 0.0
    long_response_gap_count = sum(1 for gap in response_gaps if gap >= LONG_PAUSE_SECONDS)

    patient_duration = sum(max(0.0, seg["end"] - seg["start"]) for seg in patient_segments)
    patient_turn_count = len(patient_segments)
    avg_patient_turn = round(patient_duration / patient_turn_count, 3) if patient_turn_count > 0 else 0.0

    student_audio_path = OUTPUT_DIR / f"{session_id}_student.wav"
    build_student_audio(audio_path, student_segments, student_audio_path)

    opensmile_features = extract_opensmile_features(student_audio_path)
    try:
        student_audio_path.unlink(missing_ok=True)
    except Exception:
        pass

    evidence = build_evidence(long_pause_evidence, overlap_evidence, response_gap_evidence)

    warnings: list[str] = []
    if student_duration < 15:
        warnings.append("Student speech is short; audio metrics may be unreliable.")
    if student_choice["confidence"] < 0.3:
        warnings.append("Student speaker identification is low confidence; verify diarization.")

    payload = {
        "schema": "audio-professionalism-v1",
        "session_id": session_id,
        "audio_file": str(audio_path.resolve()),
        "transcript_file": str(transcript_path.resolve()),
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "student_speaker": student_choice,
        "metrics": {
            "student_talk_time_sec": round(student_duration, 3),
            "total_talk_time_sec": round(total_duration, 3),
            "student_talk_ratio": round(student_duration / total_duration, 3),
            "patient_talk_time_sec": round(patient_duration, 3),
            "patient_talk_ratio": round(patient_duration / total_duration, 3),
            "words_per_minute": round(student_wpm, 2),
            "filler_words_per_minute": round(filler_per_minute, 2),
            "filler_word_count": filler_count,
            "long_student_pauses_over_2s": long_pause_count,
            "mean_response_gap_sec": mean_response_gap,
            "long_response_gaps_over_2s": long_response_gap_count,
            "interruptions_count": overlap_count,
            "overlap_time_sec": overlap_seconds,
            "patient_turn_count": patient_turn_count,
            "avg_patient_turn_sec": avg_patient_turn,
        },
        "audio_features": opensmile_features,
        "evidence_timestamps": evidence,
        "not_observable_from_audio": NOT_OBSERVABLE_FROM_AUDIO,
        "warnings": warnings,
    }

    output_path = Path(args.output).expanduser().resolve() if args.output else OUTPUT_DIR / f"{session_id}.json"
    write_text_atomic(output_path, json.dumps(payload, indent=2) + "\n")

    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    run_main(main, script_name="audio_professionalism_extractor")
