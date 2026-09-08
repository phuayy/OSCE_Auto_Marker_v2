#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Detect bell transition timestamps from an audio file and return "
            "student clip ranges."
        )
    )
    parser.add_argument("--audio", required=True, help="Path to input audio file")
    parser.add_argument(
        "--video-duration",
        required=True,
        type=float,
        help="Full source video duration in seconds",
    )
    parser.add_argument(
        "--end-offset",
        type=float,
        default=5.0,
        help="Seconds to extend clip end after the next bell",
    )
    parser.add_argument(
        "--min-clip-seconds",
        type=float,
        default=0.5,
        help="Minimum clip duration to keep",
    )
    parser.add_argument(
        "--min-bell-gap",
        type=float,
        default=12.0,
        help="Minimum seconds between bell events",
    )
    parser.add_argument(
        "--expected-bells",
        type=int,
        default=0,
        help="Optional expected number of bell events (0 = auto)",
    )
    parser.add_argument(
        "--bell-sample",
        default="",
        help="Optional bell reference WAV path used to lock target frequency",
    )
    parser.add_argument(
        "--pairing-mode",
        choices=["pair", "continuous"],
        default="pair",
        help=(
            "How to turn bell timestamps into clips. 'pair' treats bells as "
            "(start, end) pairs per student (default). 'continuous' emits one "
            "clip per bell (legacy behavior)."
        ),
    )
    parser.add_argument(
        "--start-offset",
        type=float,
        default=0.0,
        help="Seconds to extend clip start before the start bell (default 0)",
    )
    parser.add_argument(
        "--detector",
        choices=["bells", "silence", "hybrid"],
        default="hybrid",
        help=(
            "Which trigger detector to use. 'bells' = pure frequency detection "
            "(legacy). 'silence' = split by long silence gaps between students. "
            "'hybrid' (default) = run bells first, then repair missing bells "
            "using silence gaps."
        ),
    )
    parser.add_argument(
        "--expected-students",
        type=int,
        default=0,
        help=(
            "Optional expected number of students. When set, the hybrid "
            "detector targets exactly 2 * expected-students bells (a start "
            "and end bell per student)."
        ),
    )
    parser.add_argument(
        "--min-silence-gap-seconds",
        type=float,
        default=6.0,
        help=(
            "Minimum duration (seconds) of a low-energy region to treat it as "
            "a between-students silence gap (default 6)."
        ),
    )
    parser.add_argument(
        "--silence-rms-percentile",
        type=float,
        default=18.0,
        help=(
            "Percentile of frame RMS energy used as the silence floor. Lower "
            "values require quieter audio to qualify as silence (default 18)."
        ),
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=44100,
        help="Sample rate to use for bell and silence detection (default 44100)",
    )
    parser.add_argument(
        "--chunk-seconds",
        type=float,
        default=60.0,
        help="Chunk size (seconds) for streaming detection on long recordings (default 60)",
    )
    return parser.parse_args()


def _zscore(values):
    import numpy as np

    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return arr
    mean = float(np.mean(arr))
    std = float(np.std(arr))
    if std <= 1e-9:
        return arr * 0.0
    return (arr - mean) / std


def _pick_local_peaks(score, min_distance_frames, threshold):
    import numpy as np

    arr = np.asarray(score, dtype=float)
    if arr.size < 3:
        return np.array([], dtype=int)

    local_max = (arr[1:-1] > arr[:-2]) & (arr[1:-1] >= arr[2:]) & (arr[1:-1] >= threshold)
    candidate_idx = np.flatnonzero(local_max) + 1
    if candidate_idx.size == 0:
        return candidate_idx

    order = sorted(candidate_idx.tolist(), key=lambda idx: float(arr[idx]), reverse=True)
    selected: list[int] = []
    for idx in order:
        if all(abs(idx - kept) >= min_distance_frames for kept in selected):
            selected.append(int(idx))

    selected.sort()
    return np.asarray(selected, dtype=int)


def _fill_large_gaps(peak_idx, score, min_distance_frames, fill_threshold):
    import numpy as np

    if len(peak_idx) < 2:
        return peak_idx

    arr = np.asarray(score, dtype=float)
    peak_idx = list(int(i) for i in peak_idx)
    frame_gaps = [peak_idx[i + 1] - peak_idx[i] for i in range(len(peak_idx) - 1)]
    if not frame_gaps:
        return peak_idx

    typical_gap = float(np.median(frame_gaps))
    if typical_gap <= 0:
        return peak_idx

    updated = peak_idx[:]
    cursor = 0
    while cursor < len(updated) - 1:
        left = updated[cursor]
        right = updated[cursor + 1]
        gap = right - left
        if gap > 1.75 * typical_gap:
            scan_start = left + min_distance_frames
            scan_end = right - min_distance_frames
            if scan_end > scan_start:
                window = arr[scan_start:scan_end]
                if window.size > 0:
                    local_idx = int(np.argmax(window))
                    candidate = scan_start + local_idx
                    if arr[candidate] >= fill_threshold:
                        updated.insert(cursor + 1, int(candidate))
                        cursor += 1
                        continue
        cursor += 1

    return updated


def _iter_audio_chunks(audio_path: Path, chunk_seconds: float, target_sr: int) -> tuple[list, float, int]:
    import numpy as np
    import soundfile as sf
    import librosa

    with sf.SoundFile(str(audio_path)) as audio_file:
        native_sr = int(audio_file.samplerate)
        chunk_frames = max(1, int(round(float(chunk_seconds) * native_sr)))
        total_frames = int(audio_file.frames)
        offset_frames = 0

        while offset_frames < total_frames:
            audio_file.seek(offset_frames)
            raw = audio_file.read(chunk_frames, dtype="float32", always_2d=False)
            if raw is None or np.asarray(raw).size == 0:
                break

            if raw.ndim > 1:
                raw = np.mean(raw, axis=1)

            chunk_offset_seconds = offset_frames / float(native_sr)
            offset_frames += int(raw.shape[0])

            if target_sr and target_sr > 0 and native_sr != target_sr:
                raw = librosa.resample(raw, orig_sr=native_sr, target_sr=target_sr)
                yield raw, chunk_offset_seconds, target_sr
            else:
                yield raw, chunk_offset_seconds, native_sr


def _select_peaks_global(candidates: list[dict], min_bell_gap: float) -> list[dict]:
    ordered = sorted(candidates, key=lambda item: float(item.get("score", 0.0)), reverse=True)
    selected: list[dict] = []

    for item in ordered:
        timestamp = float(item.get("time", 0.0))
        if all(abs(timestamp - float(existing.get("time", 0.0))) >= min_bell_gap for existing in selected):
            selected.append(item)

    return selected


def estimate_bell_frequency_from_audio(
    audio_path: Path,
    target_sr: int,
    chunk_seconds: float,
    sample_points: int = 3,
) -> tuple[float | None, dict]:
    import librosa
    import numpy as np
    import soundfile as sf

    try:
        with sf.SoundFile(str(audio_path)) as audio_file:
            total_seconds = float(audio_file.frames) / float(audio_file.samplerate)
            source_sr = int(audio_file.samplerate)
    except RuntimeError:
        return None, {"reason": "unable to open audio file"}

    window_seconds = min(20.0, max(5.0, float(chunk_seconds) * 0.5))

    if total_seconds <= window_seconds:
        offsets = [0.0]
    else:
        middle = max(0.0, (total_seconds - window_seconds) / 2.0)
        tail = max(0.0, total_seconds - window_seconds)
        offsets = [0.0, middle, tail][: max(1, sample_points)]

    hist_edges = np.arange(1800, 6000 + 50, 50, dtype=float)
    hist_values = np.zeros(hist_edges.size - 1, dtype=float)

    n_fft = 4096
    hop = 256

    for offset in offsets:
        y, sr = librosa.load(
            str(audio_path),
            sr=target_sr if target_sr and target_sr > 0 else None,
            mono=True,
            offset=float(offset),
            duration=window_seconds,
        )
        if y.size == 0:
            continue

        y = librosa.util.normalize(y)
        stft = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=hop))
        if stft.size == 0:
            continue

        freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
        band = (freqs >= 1800) & (freqs <= 6000)
        if not np.any(band):
            continue

        band_stft = stft[band, :]
        band_freqs = freqs[band]
        if band_stft.size == 0:
            continue

        peak_bin_idx = np.argmax(band_stft, axis=0)
        peak_freqs = band_freqs[peak_bin_idx]
        peak_power = band_stft[peak_bin_idx, np.arange(band_stft.shape[1])]

        window_hist, _ = np.histogram(peak_freqs, bins=hist_edges, weights=peak_power)
        if window_hist.size == hist_values.size:
            hist_values += window_hist

    if hist_values.size == 0 or float(np.max(hist_values)) <= 0:
        return None, {"reason": "unable to estimate bell frequency"}

    target_bin = int(np.argmax(hist_values))
    bell_freq = float((hist_edges[target_bin] + hist_edges[target_bin + 1]) / 2.0)

    return bell_freq, {
        "source": str(audio_path),
        "sample_rate": int(target_sr if target_sr and target_sr > 0 else source_sr),
        "window_seconds": window_seconds,
        "sample_points": len(offsets),
        "estimated_bell_frequency_hz": bell_freq,
    }


def estimate_bell_frequency_from_sample(sample_path: Path) -> tuple[float | None, dict]:
    import librosa
    import numpy as np

    y, sr = librosa.load(str(sample_path), sr=None, mono=True)
    if y.size == 0:
        return None, {"reason": "empty bell sample"}

    y = librosa.util.normalize(y)
    n_fft = 4096
    hop = 256
    stft = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=hop))
    if stft.size == 0:
        return None, {"reason": "empty bell sample stft"}

    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    band = (freqs >= 1800) & (freqs <= 6000)
    if not np.any(band):
        return None, {"reason": "invalid bell sample frequency band"}

    band_stft = stft[band, :]
    band_freqs = freqs[band]
    avg_spec = np.mean(band_stft, axis=1)
    if avg_spec.size == 0 or float(np.max(avg_spec)) <= 0:
        return None, {"reason": "bell sample has no spectral peak"}

    top_idx = np.argsort(avg_spec)[-8:]
    top_freqs = band_freqs[top_idx]
    top_weights = avg_spec[top_idx]
    bell_freq = float(np.sum(top_freqs * top_weights) / np.maximum(np.sum(top_weights), 1e-9))

    return bell_freq, {
        "source": str(sample_path),
        "sample_rate": int(sr),
        "n_fft": int(n_fft),
        "estimated_bell_frequency_hz": bell_freq,
    }


def detect_bells(
    audio_path: Path,
    min_bell_gap: float,
    expected_bells: int = 0,
    bell_sample_path: Path | None = None,
    sample_rate: int = 44100,
    chunk_seconds: float = 60.0,
) -> tuple[list[float], dict]:
    import librosa
    import numpy as np

    target_sr = int(sample_rate) if sample_rate and sample_rate > 0 else 44100
    sample_debug = {}
    bell_freq = None
    if bell_sample_path and bell_sample_path.exists():
        bell_freq, sample_debug = estimate_bell_frequency_from_sample(bell_sample_path)

    if bell_freq is None:
        bell_freq, sample_debug = estimate_bell_frequency_from_audio(
            audio_path,
            target_sr=target_sr,
            chunk_seconds=chunk_seconds,
        )
        if bell_freq is None:
            return [], {"reason": "unable to estimate bell frequency"}

    half_width = 140.0
    n_fft = 4096
    hop = 512
    hop_seconds = float(hop / target_sr)

    freqs = librosa.fft_frequencies(sr=target_sr, n_fft=n_fft)
    bell_search_band = (freqs >= 1800) & (freqs <= 6000)
    if not np.any(bell_search_band):
        return [], {"reason": "bell search band unavailable"}

    primary_band = (freqs >= bell_freq - half_width) & (freqs <= bell_freq + half_width)
    harmonic_freq = bell_freq * 2.0
    harmonic_band = (freqs >= harmonic_freq - 180.0) & (freqs <= harmonic_freq + 180.0)

    speech_band = (freqs >= 150) & (freqs <= 1800)

    candidates: list[dict] = []
    chunk_debug: list[dict] = []
    min_distance_frames = max(1, int(round(float(min_bell_gap) / hop_seconds)))

    for chunk, offset_seconds, sr in _iter_audio_chunks(audio_path, chunk_seconds, target_sr):
        if chunk.size < n_fft:
            continue

        chunk = librosa.util.normalize(chunk)
        stft = np.abs(librosa.stft(chunk, n_fft=n_fft, hop_length=hop))
        if stft.size == 0:
            continue

        times = librosa.frames_to_time(np.arange(stft.shape[1]), sr=sr, hop_length=hop)
        band_stft = stft[bell_search_band, :]
        if band_stft.size == 0:
            continue

        peak_bin_idx = np.argmax(band_stft, axis=0)
        peak_power = band_stft[peak_bin_idx, np.arange(band_stft.shape[1])]

        total_energy = np.maximum(np.mean(stft, axis=0), 1e-8)
        primary_energy = (
            np.mean(stft[primary_band, :], axis=0) if np.any(primary_band) else np.zeros_like(total_energy)
        )
        harmonic_energy = (
            np.mean(stft[harmonic_band, :], axis=0) if np.any(harmonic_band) else np.zeros_like(total_energy)
        )
        speech_energy = (
            np.mean(stft[speech_band, :], axis=0) if np.any(speech_band) else np.zeros_like(total_energy)
        )

        tonality = peak_power / np.maximum(np.mean(band_stft, axis=0), 1e-8)
        bell_energy_ratio = (primary_energy + 0.35 * harmonic_energy) / total_energy
        speech_suppressed_ratio = bell_energy_ratio / np.maximum(speech_energy / total_energy, 1e-6)
        bell_flux = np.maximum(0.0, np.diff(bell_energy_ratio, prepend=bell_energy_ratio[0]))

        score = (
            0.48 * _zscore(speech_suppressed_ratio)
            + 0.27 * _zscore(bell_energy_ratio)
            + 0.15 * _zscore(tonality)
            + 0.10 * _zscore(bell_flux)
        )
        score = np.convolve(score, np.ones(7) / 7.0, mode="same")

        score_mean = float(np.mean(score))
        score_std = float(np.std(score))
        q98 = float(np.quantile(score, 0.98))
        strict_threshold = max(score_mean + 1.65 * score_std, q98)
        relaxed_threshold = max(score_mean + 1.10 * score_std, float(np.quantile(score, 0.95)))

        selected_idx = _pick_local_peaks(score, min_distance_frames, strict_threshold)
        if selected_idx.size < 2:
            selected_idx = _pick_local_peaks(score, min_distance_frames, relaxed_threshold)

        for idx in selected_idx.tolist():
            candidates.append(
                {
                    "time": float(offset_seconds + float(times[idx])),
                    "score": float(score[idx]),
                }
            )

        chunk_debug.append(
            {
                "offset_seconds": float(offset_seconds),
                "score_mean": score_mean,
                "score_std": score_std,
                "strict_threshold": strict_threshold,
                "relaxed_threshold": relaxed_threshold,
                "peak_count": int(selected_idx.size),
            }
        )

    if not candidates:
        return [], {
            "reason": "no bell peaks",
            "bell_frequency_hz": bell_freq,
        }

    selected_by_score = _select_peaks_global(candidates, float(min_bell_gap))
    if expected_bells > 0 and len(selected_by_score) > expected_bells:
        selected_by_score = selected_by_score[:expected_bells]

    selected_by_time = sorted(selected_by_score, key=lambda item: float(item.get("time", 0.0)))
    bell_timestamps = [float(item.get("time", 0.0)) for item in selected_by_time]

    return bell_timestamps, {
        "bell_frequency_hz": bell_freq,
        "used_bell_sample": bool(bell_sample_path and bell_sample_path.exists()),
        "bell_sample_debug": sample_debug,
        "candidate_count": int(len(candidates)),
        "selected_count": int(len(bell_timestamps)),
        "chunk_seconds": float(chunk_seconds),
        "hop_seconds": hop_seconds,
        "chunk_debug": chunk_debug[:5],
    }


def detect_silence_gaps(
    audio_path: Path,
    min_gap_seconds: float = 6.0,
    rms_percentile: float = 18.0,
    edge_trim_seconds: float = 1.0,
    sample_rate: int = 22050,
    chunk_seconds: float = 60.0,
) -> tuple[list[dict], dict]:
    """Find extended low-energy regions in the audio.

    Returns a list of {"start": float, "end": float, "duration": float} gaps
    longer than ``min_gap_seconds``. Between-student silence in OSCE videos
    falls naturally into these gaps and is independent of buzzer quality.
    """

    import librosa
    import numpy as np

    target_sr = int(sample_rate) if sample_rate and sample_rate > 0 else 22050
    frame_length = 2048
    hop_length = 512
    rms_values: list[float] = []
    time_values: list[float] = []

    for chunk, offset_seconds, sr in _iter_audio_chunks(audio_path, chunk_seconds, target_sr):
        if chunk.size < frame_length:
            continue

        rms_chunk = librosa.feature.rms(y=chunk, frame_length=frame_length, hop_length=hop_length)[0]
        if rms_chunk.size == 0:
            continue

        times_chunk = librosa.frames_to_time(
            np.arange(rms_chunk.size),
            sr=sr,
            hop_length=hop_length,
        )

        rms_values.extend(rms_chunk.tolist())
        time_values.extend((times_chunk + float(offset_seconds)).tolist())

    if not rms_values:
        return [], {"reason": "empty rms"}

    rms = np.asarray(rms_values, dtype=float)
    times = np.asarray(time_values, dtype=float)
    hop_seconds = float(hop_length / target_sr)
    min_gap_frames = max(1, int(round(float(min_gap_seconds) / hop_seconds)))

    # Adaptive silence threshold: quieter than the Nth percentile of frames,
    # with a small absolute floor so very quiet recordings still have a ceiling.
    pctile = float(np.clip(rms_percentile, 1.0, 50.0))
    threshold = float(np.percentile(rms, pctile))
    threshold = max(threshold, float(np.median(rms) * 0.35))

    silent_mask = rms <= threshold

    gaps: list[dict] = []
    cursor = 0
    total_frames = silent_mask.size
    while cursor < total_frames:
        if not silent_mask[cursor]:
            cursor += 1
            continue

        run_end = cursor
        while run_end < total_frames and silent_mask[run_end]:
            run_end += 1

        run_length = run_end - cursor
        if run_length >= min_gap_frames:
            start_time = float(times[cursor])
            end_time = float(times[run_end - 1] + hop_seconds)

            trimmed_start = start_time + max(0.0, float(edge_trim_seconds))
            trimmed_end = end_time - max(0.0, float(edge_trim_seconds))
            if trimmed_end - trimmed_start >= min_gap_seconds * 0.5:
                gaps.append(
                    {
                        "start": trimmed_start,
                        "end": trimmed_end,
                        "duration": trimmed_end - trimmed_start,
                    }
                )

        cursor = run_end

    debug = {
        "silence_threshold_rms": threshold,
        "rms_percentile": pctile,
        "frame_count": int(total_frames),
        "hop_seconds": hop_seconds,
        "min_gap_seconds": float(min_gap_seconds),
        "gap_count": len(gaps),
    }
    return gaps, debug


def build_clip_ranges_from_silence(
    silence_gaps: list[dict],
    video_duration: float,
    end_offset: float,
    start_offset: float,
    min_clip_seconds: float,
) -> list[dict]:
    """Turn silence gaps into student clips.

    Student N plays between silence_gap[N-1].end and silence_gap[N].start.
    The first student spans [0, silence_gap[0].start] and the last student
    spans [silence_gap[-1].end, video_duration].
    """

    if video_duration <= 0:
        return []

    sorted_gaps = sorted(
        (
            (float(item.get("start", 0.0)), float(item.get("end", 0.0)))
            for item in silence_gaps or []
        ),
        key=lambda gap: gap[0],
    )

    boundaries = [0.0]
    for gap_start, gap_end in sorted_gaps:
        boundaries.append(gap_start)
        boundaries.append(gap_end)
    boundaries.append(float(video_duration))

    clip_ranges: list[dict] = []
    for pair_index, i in enumerate(range(0, len(boundaries), 2)):
        if i + 1 >= len(boundaries):
            break

        segment_start = max(0.0, boundaries[i])
        segment_end = min(float(video_duration), boundaries[i + 1])

        if segment_end - segment_start < min_clip_seconds:
            continue

        # The silence trim already removed the quiet lead-in. We still honour
        # the offsets so the end_offset lets a bit of the buzzer ring through.
        effective_start = max(0.0, segment_start - max(0.0, float(start_offset)))
        effective_end = min(float(video_duration), segment_end + max(0.0, float(end_offset)))

        if effective_end - effective_start < min_clip_seconds:
            continue

        clip_ranges.append(
            {
                "start": float(effective_start),
                "end": float(effective_end),
                "student_index": pair_index + 1,
                "start_bell": None,
                "end_bell": None,
                "source_trigger": "silence_gap",
            }
        )

    return clip_ranges


def repair_bells_with_silence(
    bells: list[float],
    silence_gaps: list[dict],
    expected_bells: int,
    min_bell_gap: float,
) -> tuple[list[float], list[dict]]:
    """Insert inferred bell timestamps at silence-gap edges when bells are missing.

    Between-student silence gaps are bounded by the end bell of student N and
    the start bell of student N+1. If a bell is missing, one edge of a silence
    gap will have no bell within ``min_bell_gap`` seconds. We insert an
    inferred bell at that edge to restore the expected count.
    """

    repairs: list[dict] = []
    if expected_bells <= 0 or not silence_gaps:
        return sorted(set(bells)), repairs

    updated = sorted(float(value) for value in bells)
    tolerance = max(2.0, float(min_bell_gap) * 0.4)

    def _nearest(ts_list: list[float], target: float) -> float | None:
        if not ts_list:
            return None
        return min(ts_list, key=lambda value: abs(value - target))

    passes = 0
    while len(updated) < expected_bells and passes < expected_bells:
        passes += 1
        inserted_this_pass = False

        for gap in silence_gaps:
            gap_start = float(gap.get("start", 0.0))
            gap_end = float(gap.get("end", 0.0))

            left_neighbor = _nearest(updated, gap_start)
            right_neighbor = _nearest(updated, gap_end)

            missing_left = left_neighbor is None or abs(left_neighbor - gap_start) > tolerance
            missing_right = right_neighbor is None or abs(right_neighbor - gap_end) > tolerance

            if missing_left and missing_right:
                # Both edges missing: insert both (end of previous student + start of next).
                updated.append(gap_start)
                updated.append(gap_end)
                repairs.append({"reason": "gap_both_edges_missing", "gap": gap})
                inserted_this_pass = True
                continue

            if missing_left and not missing_right:
                updated.append(gap_start)
                repairs.append({"reason": "gap_left_edge_missing", "gap": gap})
                inserted_this_pass = True
                continue

            if missing_right and not missing_left:
                updated.append(gap_end)
                repairs.append({"reason": "gap_right_edge_missing", "gap": gap})
                inserted_this_pass = True
                continue

            if len(updated) >= expected_bells:
                break

        if not inserted_this_pass:
            break

        updated = sorted(updated)

    return sorted(set(updated)), repairs


def build_clip_ranges(
    bells: list[float],
    video_duration: float,
    end_offset: float,
    min_clip_seconds: float,
    pairing_mode: str = "pair",
    start_offset: float = 0.0,
) -> list[dict]:
    clip_ranges: list[dict] = []
    if video_duration <= 0:
        return clip_ranges

    cleaned_bells = sorted({max(0.0, min(float(ts), video_duration)) for ts in bells})
    if not cleaned_bells:
        return clip_ranges

    mode = str(pairing_mode or "pair").strip().lower()

    if mode == "pair":
        # OSCE pattern: bells ring in (start, end) pairs. Bell[0] = student 1 walks in,
        # Bell[1] = student 1 session over, [silence], Bell[2] = student 2 walks in, ...
        # We emit one clip per pair and skip the silence gap between students.
        for pair_index, i in enumerate(range(0, len(cleaned_bells), 2)):
            start_bell = cleaned_bells[i]
            start = max(0.0, start_bell - max(0.0, float(start_offset)))

            if i + 1 < len(cleaned_bells):
                end_bell = cleaned_bells[i + 1]
                end = min(video_duration, end_bell + end_offset)
            else:
                end_bell = None
                end = video_duration

            if end - start >= min_clip_seconds:
                clip_ranges.append(
                    {
                        "start": float(start),
                        "end": float(end),
                        "student_index": pair_index + 1,
                        "start_bell": float(start_bell),
                        "end_bell": float(end_bell) if end_bell is not None else None,
                    }
                )
        return clip_ranges

    # Legacy continuous mode: every bell starts a new clip running to the next bell.
    for idx, start in enumerate(cleaned_bells):
        next_bell = cleaned_bells[idx + 1] if idx + 1 < len(cleaned_bells) else None
        end = video_duration if next_bell is None else min(video_duration, next_bell + end_offset)

        if end - start >= min_clip_seconds:
            clip_ranges.append({"start": float(start), "end": float(end)})

    return clip_ranges


def main() -> int:
    args = parse_args()
    audio_path = Path(args.audio).expanduser().resolve()
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")
    bell_sample_path = Path(args.bell_sample).expanduser().resolve() if str(args.bell_sample or "").strip() else None

    detector_mode = str(args.detector or "hybrid").strip().lower()
    expected_students = max(0, int(args.expected_students or 0))

    # If the caller specified expected_students but not expected_bells, derive
    # the expected bell count (2 per student) so the bell detector can prune.
    expected_bells = max(0, int(args.expected_bells or 0))
    if expected_bells == 0 and expected_students > 0:
        expected_bells = expected_students * 2

    bell_timestamps: list[float] = []
    bell_debug: dict = {}
    silence_gaps: list[dict] = []
    silence_debug: dict = {}
    repairs: list[dict] = []

    try:
        if detector_mode in ("bells", "hybrid"):
            bell_timestamps, bell_debug = detect_bells(
                audio_path,
                float(args.min_bell_gap),
                expected_bells=expected_bells,
                bell_sample_path=bell_sample_path,
                sample_rate=int(args.sample_rate),
                chunk_seconds=float(args.chunk_seconds),
            )

        if detector_mode in ("silence", "hybrid"):
            silence_gaps, silence_debug = detect_silence_gaps(
                audio_path,
                min_gap_seconds=float(args.min_silence_gap_seconds),
                rms_percentile=float(args.silence_rms_percentile),
                sample_rate=int(args.sample_rate),
                chunk_seconds=float(args.chunk_seconds),
            )
    except ImportError as error:
        raise RuntimeError(
            "Missing bell detector dependencies. Install with: uv sync"
        ) from error

    used_trigger = detector_mode
    effective_bells = list(bell_timestamps)

    if detector_mode == "hybrid":
        # Repair missing bells using silence-gap edges, then validate the count.
        if expected_bells > 0 and silence_gaps:
            effective_bells, repairs = repair_bells_with_silence(
                bell_timestamps,
                silence_gaps,
                expected_bells=expected_bells,
                min_bell_gap=float(args.min_bell_gap),
            )

        bell_count_ok = (
            len(effective_bells) == expected_bells if expected_bells > 0 else (len(effective_bells) % 2 == 0 and len(effective_bells) >= 2)
        )

        if not bell_count_ok and silence_gaps:
            # Bell-based pairing is unreliable; fall back to pure silence split.
            clip_ranges = build_clip_ranges_from_silence(
                silence_gaps=silence_gaps,
                video_duration=float(args.video_duration),
                end_offset=float(args.end_offset),
                start_offset=float(args.start_offset),
                min_clip_seconds=float(args.min_clip_seconds),
            )
            used_trigger = "hybrid_fallback_silence"
        else:
            clip_ranges = build_clip_ranges(
                bells=effective_bells,
                video_duration=float(args.video_duration),
                end_offset=float(args.end_offset),
                min_clip_seconds=float(args.min_clip_seconds),
                pairing_mode=str(args.pairing_mode),
                start_offset=float(args.start_offset),
            )
            used_trigger = "hybrid_bells" if not repairs else "hybrid_bells_repaired"

    elif detector_mode == "silence":
        clip_ranges = build_clip_ranges_from_silence(
            silence_gaps=silence_gaps,
            video_duration=float(args.video_duration),
            end_offset=float(args.end_offset),
            start_offset=float(args.start_offset),
            min_clip_seconds=float(args.min_clip_seconds),
        )

    else:  # "bells"
        clip_ranges = build_clip_ranges(
            bells=effective_bells,
            video_duration=float(args.video_duration),
            end_offset=float(args.end_offset),
            min_clip_seconds=float(args.min_clip_seconds),
            pairing_mode=str(args.pairing_mode),
            start_offset=float(args.start_offset),
        )

    # If the caller told us how many students to expect, enforce the count by
    # keeping the N longest clips (or padding from silence if we have extras).
    if expected_students > 0 and len(clip_ranges) > expected_students:
        clip_ranges = sorted(
            clip_ranges,
            key=lambda item: float(item.get("end", 0.0)) - float(item.get("start", 0.0)),
            reverse=True,
        )[:expected_students]
        clip_ranges.sort(key=lambda item: float(item.get("start", 0.0)))
        for index, item in enumerate(clip_ranges, start=1):
            item["student_index"] = index

    student_count = sum(1 for item in clip_ranges if "student_index" in item)

    payload = {
        "detector": "osce-trigger-ensemble-v1",
        "detector_mode": detector_mode,
        "used_trigger": used_trigger,
        "audio_file": str(audio_path),
        "video_duration": float(args.video_duration),
        "end_offset": float(args.end_offset),
        "start_offset": float(args.start_offset),
        "min_clip_seconds": float(args.min_clip_seconds),
        "min_bell_gap": float(args.min_bell_gap),
        "expected_bells": expected_bells,
        "expected_students": expected_students,
        "pairing_mode": str(args.pairing_mode),
        "min_silence_gap_seconds": float(args.min_silence_gap_seconds),
        "bell_sample": str(bell_sample_path) if bell_sample_path else None,
        "bell_timestamps": bell_timestamps,
        "effective_bell_timestamps": effective_bells,
        "bell_count": len(bell_timestamps),
        "silence_gaps": silence_gaps,
        "silence_gap_count": len(silence_gaps),
        "bell_repairs": repairs,
        "student_count": student_count,
        "clip_ranges": clip_ranges,
        "debug": {
            "bells": bell_debug,
            "silence": silence_debug,
        },
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
