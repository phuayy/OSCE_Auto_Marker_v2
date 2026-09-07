from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.config import Settings
from app.core.exceptions import EmptyTranscriptError
from app.core.json_utils import extract_json_object
from app.core.process import CommandRunner
from app.core.utils import atomic_replace, clamp_number, format_timestamp, utc_now_iso
from app.pipeline.whisperx_options import WhisperxRunOptions
from app.pipeline.progress_tracker import ProgressTracker
from app.services.auth_service import AuthService
from app.services.event_service import EventService

# Called with the WhisperX step's overall completion percentage (0-100) each
# time it advances. Awaited, so a handler may persist the value.
ProgressCallback = Callable[[float], Awaitable[None]]

# Value of the raw transcript's "engine" marker for WhisperX. WhisperX writes
# no such key, so its absence means WhisperX and any other value means the
# artifact belongs to a different engine.
ENGINE_MARKER_WHISPERX = "whisperx"


class MediaPipeline:
    def __init__(
        self,
        settings: Settings,
        runner: CommandRunner,
        events: EventService,
        auth: AuthService,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.events = events
        self.auth = auth

    @staticmethod
    def count_usable_segments(payload: Any) -> int:
        """Number of WhisperX segments carrying non-empty text.

        Applies exactly the rule ``normalize_whisperx_transcript`` uses to keep
        a segment, so a payload this reports as usable cannot normalize down to
        an empty transcript. A syntactically valid JSON document is *not*
        evidence of a usable transcription: WhisperX writes
        ``{"segments": []}`` for silent or failed audio, and that artifact would
        otherwise be cached and scored as if it were a real transcript.
        """
        if not isinstance(payload, dict):
            return 0
        segments = payload.get("segments")
        if not isinstance(segments, list):
            return 0
        return sum(
            1
            for segment in segments
            if isinstance(segment, dict) and str(segment.get("text") or "").strip()
        )

    @staticmethod
    def infer_speaker(segment: dict[str, Any]) -> str:
        speaker = segment.get("speaker")
        if speaker:
            return str(speaker)
        words = segment.get("words")
        if isinstance(words, list):
            for word in words:
                if isinstance(word, dict) and word.get("speaker"):
                    return str(word["speaker"])
        return "SPEAKER_UNKNOWN"

    def normalize_whisperx_transcript(self, raw_whisperx_json: dict[str, Any]) -> dict[str, Any]:
        raw_segments = raw_whisperx_json.get("segments")
        segments: list[dict[str, Any]] = []
        if isinstance(raw_segments, list):
            for index, raw_segment in enumerate(raw_segments):
                if not isinstance(raw_segment, dict):
                    continue
                start = float(raw_segment.get("start") or 0)
                end = float(raw_segment.get("end") if raw_segment.get("end") is not None else start)
                text = str(raw_segment.get("text") or "").strip()
                if not text:
                    continue
                segment: dict[str, Any] = {
                    "id": index + 1,
                    "speaker": self.infer_speaker(raw_segment),
                    "start": start,
                    "end": end,
                    "startLabel": format_timestamp(start),
                    "endLabel": format_timestamp(end),
                    "text": text,
                }
                # Carried through so hallucination screening can use the
                # decoder's own confidence. Engines that report none (Canary-
                # Qwen) simply omit the field and are screened on text alone.
                avg_logprob = raw_segment.get("avg_logprob")
                if isinstance(avg_logprob, (int, float)) and not isinstance(avg_logprob, bool):
                    segment["avgLogprob"] = float(avg_logprob)
                segments.append(segment)

        return {
            "schema": "whisperx-segments-v1",
            "source": "whisperx-local",
            "generatedAt": utc_now_iso(),
            "language": raw_whisperx_json.get("language"),
            "segmentCount": len(segments),
            "segments": segments,
        }

    @staticmethod
    def convert_srt_text_to_vtt_text(srt_text: str) -> str:
        normalized = str(srt_text or "").replace("\r\n", "\n").replace("\r", "\n")
        converted = [
            line.replace(",", ".") if "-->" in line else line
            for line in normalized.split("\n")
        ]
        return "WEBVTT\n\n" + "\n".join(converted) + "\n"

    async def create_vtt_from_srt(self, srt_path: Path) -> Path:
        def _convert() -> Path:
            raw = srt_path.read_text(encoding="utf-8")
            vtt_path = srt_path.with_suffix(".vtt")
            vtt_path.write_text(self.convert_srt_text_to_vtt_text(raw), encoding="utf-8")
            return vtt_path

        return await asyncio.to_thread(_convert)

    async def ensure_session_subtitle_track(self, session: dict[str, Any]) -> bool:
        if not session.get("id"):
            return False
        outputs = session.get("outputs") or {}
        subtitle_track_path = (outputs.get("subtitleTrack") or {}).get("absolutePath")
        if subtitle_track_path and Path(str(subtitle_track_path)).exists():
            return False

        srt_path_raw = (outputs.get("subtitle") or {}).get("absolutePath")
        if not srt_path_raw:
            return False
        srt_path = Path(str(srt_path_raw))
        if not srt_path.exists():
            return False

        vtt_path = await self.create_vtt_from_srt(srt_path)
        stats = vtt_path.stat()
        session.setdefault("outputs", {})["subtitleTrack"] = {
            "fileName": vtt_path.name,
            "absolutePath": str(vtt_path),
            "url": f"/media/whisperx/{session['id']}/{vtt_path.name}",
            "sizeBytes": stats.st_size,
        }
        return True

    async def find_latest_output_file(
        self,
        output_dir: Path,
        extension: str,
        preferred_base_name: str = "",
    ) -> Path | None:
        normalized_extension = extension.lower()
        if not normalized_extension.startswith("."):
            normalized_extension = f".{normalized_extension}"
        if preferred_base_name:
            preferred = output_dir / f"{preferred_base_name}{normalized_extension}"
            if preferred.exists():
                return preferred
        if not output_dir.exists():
            return None

        def _latest() -> Path | None:
            matching = [
                item
                for item in output_dir.iterdir()
                if item.is_file() and item.name.lower().endswith(normalized_extension)
            ]
            if not matching:
                return None
            return sorted(matching, key=lambda item: item.stat().st_mtime, reverse=True)[0]

        return await asyncio.to_thread(_latest)

    async def find_existing_whisperx_outputs(
        self,
        session: dict[str, Any],
        audio_info: dict[str, Any],
    ) -> dict[str, Path | None] | None:
        output_dir = self.settings.paths.output_whisperx_dir / str(session["id"])
        output_base_name = Path(str(audio_info["fileName"])).stem
        json_path = await self.find_latest_output_file(output_dir, "json", output_base_name)
        if not json_path:
            return None

        def _has_usable_transcript(path: Path) -> bool:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return False
            # Engines share this artifact directory, so an artifact another
            # engine wrote must not be served as a WhisperX resume — switching
            # engines and re-running would silently reuse the old transcript.
            # WhisperX's own output carries no marker, hence the empty default.
            if str((payload or {}).get("engine") or ENGINE_MARKER_WHISPERX) != ENGINE_MARKER_WHISPERX:
                return False
            return MediaPipeline.count_usable_segments(payload) > 0

        if not await asyncio.to_thread(_has_usable_transcript, json_path):
            return None

        srt_path = await self.find_latest_output_file(output_dir, "srt", output_base_name)
        vtt_path = await self.find_latest_output_file(output_dir, "vtt", output_base_name)
        if not vtt_path and srt_path:
            vtt_path = await self.create_vtt_from_srt(srt_path)
            await self.events.publish(
                str(session["id"]),
                "log",
                {"source": "subtitle", "message": f"Generated browser subtitle track {vtt_path.name} from SRT."},
            )
        return {"jsonAbsolutePath": json_path, "srtAbsolutePath": srt_path, "vttAbsolutePath": vtt_path}

    async def extract_audio_to_mp3(self, session: dict[str, Any]) -> dict[str, Any]:
        audio_file_name = f"{session['id']}.mp3"
        audio_path = self.settings.paths.output_audio_dir / audio_file_name
        args = [
            "-y",
            "-i",
            str(session["files"]["video"]["absolutePath"]),
            "-vn",
            "-codec:a",
            "libmp3lame",
            "-q:a",
            self.settings.audio_mp3_vbr_quality,
            "-ar",
            self.settings.audio_mp3_sample_rate,
            str(audio_path),
        ]
        await self.runner.run(self.settings.ffmpeg_bin, args, "Audio extraction (ffmpeg)")
        stats = audio_path.stat()
        return {
            "fileName": audio_file_name,
            "absolutePath": str(audio_path),
            "url": f"/media/audio/{audio_file_name}",
            "sizeBytes": stats.st_size,
        }

    async def create_bell_detection_wav(self, source_path: Path, sample_rate: int) -> Path:
        wav_path = source_path.with_name(f"{source_path.stem}.bell.wav")
        args = [
            "-y",
            "-i",
            str(source_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-sample_fmt",
            "s16",
            str(wav_path),
        ]
        await self.runner.run(self.settings.ffmpeg_bin, args, "Bell detector WAV conversion (ffmpeg)")
        return wav_path

    async def get_video_duration_seconds(self, video_path: Path) -> float:
        args = [
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ]
        result = await self.runner.run(self.settings.ffprobe_bin, args, "Video duration (ffprobe)")
        try:
            duration = float(str(result.stdout or "").strip())
        except ValueError as error:
            raise RuntimeError(f"Could not determine video duration for {video_path}") from error
        if duration <= 0:
            raise RuntimeError(f"Could not determine video duration for {video_path}")
        return duration

    async def crop_video_segment(
        self,
        *,
        input_path: Path,
        start_seconds: float,
        end_seconds: float,
        output_path: Path,
    ) -> bool:
        duration_seconds = float(end_seconds - start_seconds)
        if duration_seconds < self.settings.auto_crop_min_clip_seconds:
            raise ValueError(f"Refusing to crop too-short segment ({duration_seconds}s).")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        # ffmpeg is given a temp name and the result is renamed into place, so a
        # crop killed halfway (process restart, cancelled job) cannot leave a
        # truncated MP4 at the final path. Anything that exists there is a clip
        # that finished, which is what lets a retried export skip it.
        staging_path = output_path.with_name(f".{output_path.name}.{uuid4().hex[:8]}.partial{output_path.suffix}")
        args_copy = [
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(float(start_seconds)),
            "-t",
            str(duration_seconds),
            "-i",
            str(input_path),
            "-c",
            "copy",
            "-map",
            "0",
            str(staging_path),
        ]
        try:
            await self.runner.run(self.settings.ffmpeg_bin, args_copy, "Video clip crop (stream copy)")
            await asyncio.to_thread(atomic_replace, staging_path, output_path)
            return True
        except RuntimeError:
            await asyncio.to_thread(staging_path.unlink, True)

        args_reencode = [
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(float(start_seconds)),
            "-t",
            str(duration_seconds),
            "-i",
            str(input_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            "-shortest",
            str(staging_path),
        ]
        try:
            await self.runner.run(self.settings.ffmpeg_bin, args_reencode, "Video clip crop (re-encode)")
        except Exception:
            await asyncio.to_thread(staging_path.unlink, True)
            raise
        await asyncio.to_thread(atomic_replace, staging_path, output_path)
        return True

    def normalize_clip_ranges(self, clip_ranges: list[dict[str, Any]], video_duration_seconds: float) -> list[dict[str, float]]:
        normalized: list[dict[str, float]] = []
        for clip_range in clip_ranges or []:
            start = clamp_number(clip_range.get("start"), 0, video_duration_seconds)
            end = clamp_number(clip_range.get("end"), 0, video_duration_seconds)
            if end - start >= self.settings.auto_crop_min_clip_seconds:
                normalized.append({"start": start, "end": end})
        normalized.sort(key=lambda item: item["start"])
        return normalized

    def normalize_timeline_segments(
        self, segments: list[dict[str, Any]], video_duration_seconds: float
    ) -> list[dict[str, Any]]:
        """Sanitize the detector's full-timeline partition (sessions + intermissions).

        Unknown kinds and non-positive spans are dropped; the rest are clamped
        to the video bounds and sorted. Intermissions may be shorter than the
        minimum clip duration — they are timeline markers, never cropped files.
        """
        normalized: list[dict[str, Any]] = []
        for segment in segments or []:
            kind = str(segment.get("kind") or "").strip().lower()
            if kind not in {"session", "intermission"}:
                continue
            start = clamp_number(segment.get("start"), 0, video_duration_seconds)
            end = clamp_number(segment.get("end"), 0, video_duration_seconds)
            if end - start <= 0:
                continue
            entry: dict[str, Any] = {"start": start, "end": end, "kind": kind}
            if isinstance(segment.get("person_count"), (int, float)):
                entry["personCount"] = int(segment["person_count"])
            if kind == "session" and segment.get("student_index") is not None:
                entry["studentIndex"] = int(segment["student_index"])
            normalized.append(entry)
        normalized.sort(key=lambda item: item["start"])
        return normalized

    def _enforce_clip_count_bounds(self, detector_label: str, clip_ranges: list[dict[str, float]]) -> None:
        """Reject a detection that produced an unusable number of clips.

        Both detectors share these bounds because both answer the same
        question: how many students are on this tape. The count is a property
        of the session, not of the detector, so a cohort of 30 is as valid as a
        cohort of 3 — ``PYTHON_BELL_MAX_CLIPS <= 0`` (the default) means no
        upper bound at all. An over-eager detector is recoverable: every range
        lands in the timeline editor as a draft the operator can merge or
        delete before any MP4 is cut. Failing the job instead leaves them with
        nothing to edit.

        Zero clips is the one genuinely fatal case, so the minimum stays.
        """
        minimum = self.settings.python_bell_min_clips
        maximum = self.settings.python_bell_max_clips
        if not clip_ranges:
            raise RuntimeError(f"{detector_label} did not produce any valid clip ranges.")
        if len(clip_ranges) < minimum:
            raise RuntimeError(
                f"{detector_label} produced {len(clip_ranges)} clips, fewer than the minimum {minimum}."
            )
        if maximum > 0 and len(clip_ranges) > maximum:
            raise RuntimeError(
                f"{detector_label} produced {len(clip_ranges)} clips, more than the maximum {maximum} "
                "(raise or clear PYTHON_BELL_MAX_CLIPS to allow larger cohorts)."
            )

    async def detect_bell_clip_ranges_with_python(
        self,
        source_path: Path,
        video_duration_seconds: float,
        *,
        sample_rate: int,
    ) -> dict[str, Any]:
        if not self.settings.enable_python_bell_detector:
            raise RuntimeError("Python bell detector is disabled (ENABLE_PYTHON_BELL_DETECTOR=false).")
        if not self.settings.bell_detector_script_path.exists():
            raise RuntimeError(f"Bell detector script not found at {self.settings.bell_detector_script_path}")

        bell_audio_path = await self.create_bell_detection_wav(source_path, sample_rate)
        args = [
            str(self.settings.bell_detector_script_path),
            "--audio",
            str(bell_audio_path),
            "--video-duration",
            str(video_duration_seconds),
            "--end-offset",
            str(self.settings.bell_end_offset_seconds),
            "--start-offset",
            str(self.settings.bell_start_offset_seconds),
            "--min-clip-seconds",
            str(self.settings.auto_crop_min_clip_seconds),
            "--min-bell-gap",
            str(self.settings.python_bell_min_gap_seconds),
            "--pairing-mode",
            self.settings.python_bell_pairing_mode,
            "--detector",
            self.settings.python_detector_mode,
            "--min-silence-gap-seconds",
            str(self.settings.python_min_silence_gap_seconds),
            "--sample-rate",
            str(sample_rate),
        ]
        if self.settings.bell_detector_chunk_seconds > 0:
            args.extend(["--chunk-seconds", str(self.settings.bell_detector_chunk_seconds)])
        if self.settings.python_bell_expected_count > 0:
            args.extend(["--expected-bells", str(self.settings.python_bell_expected_count)])
        if self.settings.python_expected_students > 0:
            args.extend(["--expected-students", str(self.settings.python_expected_students)])
        if self.settings.paths.bell_sample_path.exists():
            args.extend(["--bell-sample", str(self.settings.paths.bell_sample_path)])

        try:
            result = await self.runner.run(
                self.settings.scorer_python_bin,
                args,
                "Bell detection (python)",
                env=self.settings.subprocess_env(),
            )
        finally:
            with contextlib.suppress(OSError):
                bell_audio_path.unlink()

        payload = extract_json_object(result.stdout)
        clip_ranges = self.normalize_clip_ranges(payload.get("clip_ranges") or [], video_duration_seconds)
        self._enforce_clip_count_bounds("Bell detector", clip_ranges)

        bell_timestamps: list[float] = []
        for value in payload.get("bell_timestamps") or []:
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            bell_timestamps.append(clamp_number(numeric, 0, video_duration_seconds))
        silence_gaps = payload.get("silence_gaps") if isinstance(payload.get("silence_gaps"), list) else []
        return {
            "clipRanges": clip_ranges,
            "source": {
                "type": "bell_detection_python_librosa",
                "detector": str(payload.get("detector") or "python-librosa"),
                "detectorMode": str(payload.get("detector_mode") or self.settings.python_detector_mode),
                "usedTrigger": str(payload.get("used_trigger") or self.settings.python_detector_mode),
                "bellCount": len(bell_timestamps),
                "bellTimestamps": bell_timestamps,
                "silenceGapCount": int(payload.get("silence_gap_count") or len(silence_gaps)),
                "bellRepairCount": len(payload.get("bell_repairs") or []) if isinstance(payload.get("bell_repairs"), list) else 0,
                "bellEndOffsetSeconds": self.settings.bell_end_offset_seconds,
                "bellStartOffsetSeconds": self.settings.bell_start_offset_seconds,
                "pairingMode": str(payload.get("pairing_mode") or self.settings.python_bell_pairing_mode),
                "expectedStudents": self.settings.python_expected_students,
                "studentCount": int(payload.get("student_count") or len(clip_ranges)),
            },
        }

    async def detect_person_clip_ranges_with_python(
        self,
        source_path: Path,
        video_duration_seconds: float,
        *,
        session_id: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Detect student clip ranges from person presence (RT-DETR).

        Runs ``scripts/detect_human_segments.py`` as a subprocess (same
        contract as the bell detector: stderr = progress, stdout = one JSON
        document). The subprocess owns the model lifecycle, so its VRAM is
        fully released on exit — WhisperX never shares the GPU with it.

        ``on_progress`` receives the run's overall completion percentage as it
        advances. This is the longest step a long-workflow session has, and the
        only thing it writes for minutes at a time, so a caller that persists
        these readings is also what keeps the session's change stream alive
        while it runs.
        """
        if not self.settings.enable_human_detector:
            raise RuntimeError("Human detector is disabled (ENABLE_HUMAN_DETECTOR=false).")
        if not self.settings.human_detector_script_path.exists():
            raise RuntimeError(f"Human detector script not found at {self.settings.human_detector_script_path}")

        args = [
            str(self.settings.human_detector_script_path),
            "--video",
            str(source_path),
            "--video-duration",
            str(video_duration_seconds),
            "--min-clip-seconds",
            str(self.settings.auto_crop_min_clip_seconds),
            "--end-offset",
            str(self.settings.human_detector_end_offset_seconds),
            "--start-offset",
            str(self.settings.human_detector_start_offset_seconds),
            "--workers",
            str(max(1, self.settings.human_detector_workers)),
        ]

        # The detector counts once, straight through, so a single span — unlike
        # WhisperX's two loops. It stops at 95: the segmentation maths and the
        # clip-list write that follow the last frame are not free.
        tracker = ProgressTracker(phase_spans=((0.0, 95.0),))

        async def stream_progress(stream: str, text: str) -> None:
            # Model download/load and analysis progress land in the live log so
            # the operator can see the detector starting up, not a silent stall.
            if session_id and stream == "stderr" and text.strip():
                await self.events.publish(
                    session_id,
                    "log",
                    {"source": "human-detector", "message": text.strip()},
                )
            if stream != "stderr":
                return
            percent = tracker.update(text)
            if percent is None:
                return
            if session_id:
                await self.events.publish(
                    session_id,
                    "progress",
                    {"step": "person_detection", "percent": percent},
                )
            if on_progress is not None:
                await on_progress(percent)

        # The handler is worth installing for the progress readings alone, so it
        # is no longer conditional on there being a session to log against.
        result = await self.runner.run(
            self.settings.scorer_python_bin,
            args,
            "Human detection (RT-DETR)",
            env=self.settings.subprocess_env(
                {
                    # Pin the script to the binaries the backend already resolved
                    # (Windows PATH quirks are handled once, in Settings.load).
                    "FFMPEG_BIN": self.settings.ffmpeg_bin,
                    "FFPROBE_BIN": self.settings.ffprobe_bin,
                }
            ),
            on_output=stream_progress,
        )

        payload = extract_json_object(result.stdout)
        clip_ranges = self.normalize_clip_ranges(payload.get("clip_ranges") or [], video_duration_seconds)
        self._enforce_clip_count_bounds("Human detector", clip_ranges)

        debug = payload.get("debug") if isinstance(payload.get("debug"), dict) else {}
        return {
            "clipRanges": clip_ranges,
            "timelineSegments": self.normalize_timeline_segments(
                payload.get("timeline_segments") or [], video_duration_seconds
            ),
            "source": {
                "type": "person_detection_rtdetr",
                "detector": str(payload.get("detector") or "osce-human-presence-rtdetr-v1"),
                "detectorMode": str(payload.get("detector_mode") or "person_presence"),
                "usedTrigger": str(payload.get("used_trigger") or "person_presence"),
                "model": str(payload.get("model") or ""),
                "device": str(payload.get("device") or ""),
                "confidence": payload.get("confidence"),
                "sampleFps": payload.get("sample_fps"),
                "sampledFrames": int(payload.get("sampled_frames") or 0),
                "minPeople": int(payload.get("min_people") or 2),
                "endAfterSeconds": payload.get("end_after_seconds"),
                "startAfterSeconds": payload.get("start_after_seconds"),
                "personCountHistogram": payload.get("person_count_histogram") or {},
                "endOffsetSeconds": self.settings.human_detector_end_offset_seconds,
                "startOffsetSeconds": self.settings.human_detector_start_offset_seconds,
                "cpuFallback": bool(debug.get("cpu_fallback")),
                "workers": int(debug.get("workers") or 1),
                "studentCount": int(payload.get("student_count") or len(clip_ranges)),
            },
        }

    def build_manual_clip_ranges(self, video_duration_seconds: float, boundaries: list[float]) -> list[dict[str, float]]:
        duration = float(video_duration_seconds or 0)
        if duration <= 0:
            return []
        boundary_set: set[float] = set()
        for value in boundaries or []:
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if 0 < numeric < duration:
                boundary_set.add(numeric)
        normalized_boundaries = sorted(boundary_set)
        points = [0.0, *normalized_boundaries, duration]
        ranges: list[dict[str, Any]] = []
        for index in range(len(points) - 1):
            start = clamp_number(points[index], 0, duration)
            end = clamp_number(points[index + 1], 0, duration)
            if end - start >= self.settings.auto_crop_min_clip_seconds:
                # segmentIndex is the PRE-filter position: labels/kinds sent by
                # the client are positional per segment, so a dropped sub-minimum
                # sliver must not shift the mapping of everything after it.
                ranges.append({"start": start, "end": end, "segmentIndex": index})
        return ranges

    @staticmethod
    def sanitize_clip_label(raw_label: Any, fallback: str) -> str:
        cleaned = str("" if raw_label is None else raw_label).strip()
        return (cleaned or fallback)[:80]

    @staticmethod
    def _clip_kind(clip_range: dict[str, Any]) -> str:
        """Segment kind for a range: "session" (assessable, gets a file) or
        "intermission" (greyed timeline marker — empty room / lone person).
        Ranges without a kind (bell detector, recrop) are sessions."""
        kind = str(clip_range.get("kind") or "").strip().lower()
        return kind if kind == "intermission" else "session"

    async def materialize_clip(
        self,
        *,
        session_id: str,
        clip: dict[str, Any],
        video_path: Path,
    ) -> dict[str, Any]:
        """Cut one draft clip out of the source video, in place on ``clip``.

        Deliberately one clip per call: exporting ten students is ten of these,
        each one durably recorded, so a crash or a retry resumes at the clip it
        stopped on instead of re-cutting the whole recording.

        Idempotent. ``crop_video_segment`` publishes its output atomically, so an
        MP4 already sitting at the expected path is by definition a finished
        crop and is adopted rather than repeated. The file name is derived from
        the clip's ``exportIndex``, which is fixed when the export plan is
        written, so the same clip resolves to the same path on every attempt.
        """
        export_index = int(clip.get("exportIndex") or 0)
        file_name = f"{session_id}-clip-{export_index + 1}.mp4"
        output_path = self.settings.paths.output_clips_dir / str(session_id) / file_name
        output_path.parent.mkdir(parents=True, exist_ok=True)

        reused = await asyncio.to_thread(self._is_finished_clip_file, output_path)
        if not reused:
            await self.crop_video_segment(
                input_path=video_path,
                start_seconds=float(clip["start"]),
                end_seconds=float(clip["end"]),
                output_path=output_path,
            )

        stats = output_path.stat()
        clip.update(
            {
                "fileName": file_name,
                "url": f"/media/clips/{session_id}/{file_name}",
                "absolutePath": str(output_path),
                "sizeBytes": stats.st_size,
                "isDraft": False,
            }
        )
        return {"clip": clip, "reused": reused}

    @staticmethod
    def _is_finished_clip_file(output_path: Path) -> bool:
        return output_path.is_file() and output_path.stat().st_size > 0

    def build_clip_drafts_from_ranges(
        self,
        clip_ranges: list[dict[str, Any]],
        video_duration_seconds: float,
        source_meta: dict[str, Any],
        label_overrides: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        overrides = label_overrides or []
        clips: list[dict[str, Any]] = []
        student_index = 0
        for index, clip_range in enumerate(clip_ranges):
            start = clamp_number(clip_range.get("start"), 0, video_duration_seconds)
            end = clamp_number(clip_range.get("end"), 0, video_duration_seconds)
            if end - start < self.settings.auto_crop_min_clip_seconds:
                continue
            kind = self._clip_kind(clip_range)
            if kind == "session":
                student_index += 1
                fallback_label = f"Student {student_index}"
            else:
                fallback_label = "Intermission"
            override = overrides[index] if index < len(overrides) else None
            clip: dict[str, Any] = {
                "id": str(uuid4()),
                "label": self.sanitize_clip_label(override, fallback_label),
                "start": start,
                "end": end,
                "kind": kind,
                "fileName": None,
                "url": None,
                "absolutePath": None,
                "sizeBytes": 0,
                "createdAt": utc_now_iso(),
                "source": source_meta,
                "isDraft": True,
            }
            if isinstance(clip_range.get("personCount"), (int, float)):
                clip["personCount"] = int(clip_range["personCount"])
            clips.append(clip)
        return clips

    async def run_whisperx_transcription(
        self,
        session: dict[str, Any],
        audio_info: dict[str, Any],
        *,
        on_progress: ProgressCallback | None = None,
        options: WhisperxRunOptions | None = None,
    ) -> dict[str, Path | None]:
        """Transcribe one session's audio with the WhisperX CLI.

        ``options`` carries the per-run configuration so two sessions can be
        transcribed concurrently with different models; omitting it uses this
        deployment's environment-variable defaults unchanged.
        """
        run_options = options or WhisperxRunOptions.from_settings(self.settings)
        output_dir = self.settings.paths.output_whisperx_dir / str(session["id"])
        output_dir.mkdir(parents=True, exist_ok=True)
        existing_outputs = await self.find_existing_whisperx_outputs(session, audio_info)
        if existing_outputs:
            await self.events.publish(
                str(session["id"]),
                "log",
                {
                    "source": "whisperx",
                    "message": "Reusing existing WhisperX JSON artifact from a previous attempt.",
                },
            )
            return existing_outputs

        output_base_name = Path(str(audio_info["fileName"])).stem
        transcription_input_path = await self.prepare_transcription_wav(audio_info, run_options.audio_filters)
        hf_token = self.auth.runtime.whisperx_hf_token
        whisperx_device = await self._resolve_whisperx_device(str(session["id"]))
        whisperx_compute_type = self._resolve_whisperx_compute_type(whisperx_device, run_options.compute_type)
        args = [
            str(transcription_input_path),
            "--model",
            run_options.model,
            "--device",
            whisperx_device,
            "--compute_type",
            whisperx_compute_type,
            "--batch_size",
            str(run_options.batch_size),
            "--diarize",
            "--hf_token",
            hf_token,
            "--language",
            run_options.language,
            "--output_dir",
            str(output_dir),
            "--output_format",
            run_options.output_format,
        ]
        args.extend(self._diarization_bounds_args(run_options.min_speakers, run_options.max_speakers))
        if run_options.chunk_size > 0:
            args.extend(["--chunk_size", str(run_options.chunk_size)])
        if run_options.print_progress:
            args.extend(["--print_progress", "True"])
        corpus_terms = (session.get("corpus") or {}).get("terms") or []
        hotwords = self.build_hotwords(corpus_terms)
        if hotwords:
            args.extend(["--hotwords", hotwords])
        if run_options.initial_prompt:
            args.extend(["--initial_prompt", run_options.initial_prompt])
        visible_args = ["***" if index > 0 and args[index - 1] == "--hf_token" else arg for index, arg in enumerate(args)]
        await self.events.publish(
            str(session["id"]),
            "log",
            {"source": "whisperx", "message": f"{self.settings.whisperx_bin} {' '.join(visible_args)}"},
        )
        await self.events.publish(
            str(session["id"]),
            "log",
            {
                "source": "whisperx",
                "message": self._whisperx_launch_message(whisperx_device),
            },
        )

        async def heartbeat() -> None:
            started = asyncio.get_running_loop().time()
            delay = max(1, self.settings.whisperx_log_heartbeat_ms / 1000)
            while True:
                await asyncio.sleep(delay)
                elapsed = int(asyncio.get_running_loop().time() - started)
                await self.events.publish(
                    str(session["id"]),
                    "log",
                    {
                        "source": "whisperx-heartbeat",
                        "message": f"WhisperX running on {whisperx_device}. Waiting for model output ({elapsed}s elapsed)...",
                    },
                )

        task = asyncio.create_task(heartbeat())
        try:
            await self.runner.run(
                self.settings.whisperx_bin,
                args,
                "WhisperX transcription",
                # WhisperX decodes its input by spawning a bare "ffmpeg", so the
                # resolved binary has to reach it as PATH, not as an argument.
                env=self.settings.subprocess_env(),
                on_output=self._build_whisperx_output_handler(str(session["id"]), on_progress),
            )
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        completed_outputs = await self.find_existing_whisperx_outputs(session, audio_info)
        if not completed_outputs:
            raise await self._describe_unusable_whisperx_output(output_dir, output_base_name)
        return completed_outputs

    def _diarization_bounds_args(self, min_speakers: int, max_speakers: int) -> list[str]:
        """``--min_speakers``/``--max_speakers`` for a station's known cast.

        Either bound at 0 (or below) is "unknown", and that flag is omitted so
        WhisperX estimates the count as before. A minimum above the maximum is
        a misconfiguration the CLI would reject after the model has already
        loaded, so it is clamped here instead.
        """
        minimum = max(int(min_speakers), 0)
        maximum = max(int(max_speakers), 0)
        if minimum and maximum:
            minimum = min(minimum, maximum)
        args: list[str] = []
        if minimum:
            args.extend(["--min_speakers", str(minimum)])
        if maximum:
            args.extend(["--max_speakers", str(maximum)])
        return args

    def _build_whisperx_output_handler(
        self,
        session_id: str,
        on_progress: ProgressCallback | None,
    ) -> Callable[[str, str], Awaitable[None]]:
        """Forward every WhisperX output line, lifting progress out of stdout.

        Every line still reaches the log stream unchanged. Lines carrying a
        ``Progress:`` reading additionally publish a ``progress`` event and
        invoke ``on_progress`` — but only when the tracker says the step has
        actually advanced, so a caller may persist each call.
        """
        tracker = ProgressTracker()

        async def handle(stream: str, text: str) -> None:
            await self.events.publish(
                session_id,
                "log",
                {"source": f"whisperx-{stream}", "message": text},
            )
            percent = tracker.update(text)
            if percent is None:
                return
            await self.events.publish(
                session_id,
                "progress",
                {"step": "whisperx", "percent": percent},
            )
            if on_progress is not None:
                await on_progress(percent)

        return handle

    async def _describe_unusable_whisperx_output(self, output_dir: Path, output_base_name: str) -> Exception:
        """Explain why a completed WhisperX run yielded nothing usable.

        The artifact lookup rejects both a missing JSON file and one holding no
        speech segments; those are different faults with different fixes, so
        report them separately instead of collapsing both into "no output file".
        """
        produced_json = await self.find_latest_output_file(output_dir, "json", output_base_name)
        if produced_json is None:
            return RuntimeError("WhisperX completed but no JSON output file was found.")
        return EmptyTranscriptError(
            f"WhisperX produced no usable speech segments in {produced_json.name}. "
            "The recording may be silent or speechless, or its audio track failed to extract."
        )

    @staticmethod
    def build_hotwords(terms: list[Any], max_chars: int = 900) -> str:
        """Comma-joined hotwords string capped to roughly Whisper's 224-token
        prompt budget (~4 chars/token); earlier corpus terms win the budget."""
        parts: list[str] = []
        total = 0
        for raw in terms:
            term = str(raw or "").strip()
            if not term:
                continue
            added = len(term) + (2 if parts else 0)
            if total + added > max_chars:
                break
            parts.append(term)
            total += added
        return ", ".join(parts)

    async def prepare_transcription_wav(self, audio_info: dict[str, Any], filters: str | None = None) -> Path:
        """Derive the dedicated ASR input WAV (16 kHz mono, filtered).

        Shared by every engine: WhisperX, Canary-Qwen and the standalone
        diarisation pass all want 16 kHz mono, and deriving it once per session
        means a second engine never re-transcodes the same audio.

        The extracted MP3 must stay untouched — the audio-professionalism scorer
        measures loudness on it, and normalizing it would corrupt that signal.
        The WAV keeps the MP3's stem so the engine's output artifacts keep the
        base name the artifact cache looks up.
        """
        source_path = Path(str(audio_info["absolutePath"]))
        filters = self.settings.whisperx_audio_filters if filters is None else filters
        if not filters:
            return source_path
        wav_path = source_path.with_suffix(".wav")
        args = [
            "-y",
            "-i",
            str(source_path),
            "-vn",
            "-af",
            filters,
            "-ac",
            "1",
            "-ar",
            "16000",
            "-sample_fmt",
            "s16",
            str(wav_path),
        ]
        await self.runner.run(self.settings.ffmpeg_bin, args, "WhisperX input WAV (ffmpeg filters)")
        return wav_path

    async def _resolve_whisperx_device(self, session_id: str) -> str:
        requested = str(self.settings.whisperx_device or "cpu").strip().lower()
        if requested in {"", "auto"}:
            return "cuda" if await asyncio.to_thread(self._cuda_available) else "cpu"
        if requested == "cuda" and not await asyncio.to_thread(self._cuda_available):
            await self.events.publish(
                session_id,
                "log",
                {
                    "source": "whisperx",
                    "message": "CUDA was requested for WhisperX but is not available; falling back to CPU.",
                },
            )
            return "cpu"
        return requested

    def _resolve_whisperx_compute_type(self, device: str, compute_type: str | None = None) -> str:
        requested = str(compute_type or self.settings.whisperx_compute_type or "float16").strip().lower()
        # CTranslate2 on CPU does not support float16; downgrade to a CPU-safe type
        # so a cuda->cpu fallback never crashes the run.
        if str(device).lower() == "cpu" and requested in {"float16", "fp16", "half", "int8_float16"}:
            return "int8"
        return requested

    @staticmethod
    def _cuda_available() -> bool:
        try:
            import torch

            return bool(torch.cuda.is_available())
        except Exception:
            return False

    @staticmethod
    def _whisperx_launch_message(device: str) -> str:
        if str(device).lower() == "cuda":
            return "WhisperX launched. CUDA model warmup + diarization setup can take 30-90 seconds before first transcript lines."
        return "WhisperX launched. CPU transcription can take several minutes for long recordings."
