from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.config import Settings
from app.core.json_utils import extract_json_object
from app.core.process import CommandRunner
from app.core.utils import clamp_number, format_timestamp, utc_now_iso
from app.services.auth_service import AuthService
from app.services.event_service import EventService


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
                segments.append(
                    {
                        "id": index + 1,
                        "speaker": self.infer_speaker(raw_segment),
                        "start": start,
                        "end": end,
                        "startLabel": format_timestamp(start),
                        "endLabel": format_timestamp(end),
                        "text": text,
                    }
                )

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

        def _is_valid_json(path: Path) -> bool:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return False
            return isinstance(payload, dict)

        if not await asyncio.to_thread(_is_valid_json, json_path):
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
            str(output_path),
        ]
        try:
            await self.runner.run(self.settings.ffmpeg_bin, args_copy, "Video clip crop (stream copy)")
            return True
        except RuntimeError:
            pass

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
            str(output_path),
        ]
        await self.runner.run(self.settings.ffmpeg_bin, args_reencode, "Video clip crop (re-encode)")
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
                env={"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
            )
        finally:
            with contextlib.suppress(OSError):
                bell_audio_path.unlink()

        payload = extract_json_object(result.stdout)
        clip_ranges = self.normalize_clip_ranges(payload.get("clip_ranges") or [], video_duration_seconds)
        if len(clip_ranges) < self.settings.python_bell_min_clips or len(clip_ranges) > self.settings.python_bell_max_clips:
            raise RuntimeError(
                f"Bell detector produced {len(clip_ranges)} clips outside allowed range "
                f"[{self.settings.python_bell_min_clips}, {self.settings.python_bell_max_clips}]."
            )
        if not clip_ranges:
            raise RuntimeError("Bell detector did not produce any valid clip ranges.")

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
    ) -> dict[str, Any]:
        """Detect student clip ranges from person presence (RT-DETR).

        Runs ``scripts/detect_human_segments.py`` as a subprocess (same
        contract as the bell detector: stderr = progress, stdout = one JSON
        document). The subprocess owns the model lifecycle, so its VRAM is
        fully released on exit — WhisperX never shares the GPU with it.
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

        async def stream_progress(stream: str, text: str) -> None:
            # Model download/load and analysis progress land in the live log so
            # the operator can see the detector starting up, not a silent stall.
            if session_id and stream == "stderr" and text.strip():
                await self.events.publish(
                    session_id,
                    "log",
                    {"source": "human-detector", "message": text.strip()},
                )

        result = await self.runner.run(
            self.settings.scorer_python_bin,
            args,
            "Human detection (RT-DETR)",
            env={
                "PYTHONUNBUFFERED": "1",
                "PYTHONIOENCODING": "utf-8",
                # Pin the script to the binaries the backend already resolved
                # (Windows PATH quirks are handled once, in Settings.load).
                "FFMPEG_BIN": self.settings.ffmpeg_bin,
                "FFPROBE_BIN": self.settings.ffprobe_bin,
            },
            on_output=stream_progress if session_id else None,
        )

        payload = extract_json_object(result.stdout)
        clip_ranges = self.normalize_clip_ranges(payload.get("clip_ranges") or [], video_duration_seconds)
        if len(clip_ranges) < self.settings.python_bell_min_clips or len(clip_ranges) > self.settings.python_bell_max_clips:
            raise RuntimeError(
                f"Human detector produced {len(clip_ranges)} clips outside allowed range "
                f"[{self.settings.python_bell_min_clips}, {self.settings.python_bell_max_clips}]."
            )
        if not clip_ranges:
            raise RuntimeError("Human detector did not produce any valid clip ranges.")

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

    async def write_video_clips_from_ranges(
        self,
        session: dict[str, Any],
        clip_ranges: list[dict[str, Any]],
        source_meta: dict[str, Any],
        label_overrides: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        video_path = Path(str(session["files"]["video"]["absolutePath"]))
        video_duration_seconds = await self.get_video_duration_seconds(video_path)
        session_clip_dir = self.settings.paths.output_clips_dir / str(session["id"])
        session_clip_dir.mkdir(parents=True, exist_ok=True)
        overrides = label_overrides or []
        clips: list[dict[str, Any]] = []
        student_index = 0
        for index, clip_range in enumerate(clip_ranges):
            start = clamp_number(clip_range["start"], 0, video_duration_seconds)
            end = clamp_number(clip_range["end"], 0, video_duration_seconds)
            if end - start < self.settings.auto_crop_min_clip_seconds:
                continue
            kind = self._clip_kind(clip_range)
            override = overrides[index] if index < len(overrides) else None
            clip: dict[str, Any] = {
                "id": str(uuid4()),
                "start": start,
                "end": end,
                "kind": kind,
                "createdAt": utc_now_iso(),
                "source": source_meta,
            }
            if isinstance(clip_range.get("personCount"), (int, float)):
                clip["personCount"] = int(clip_range["personCount"])
            if kind == "intermission":
                # Timeline marker only — never cropped to a file, never assessable.
                clip.update(
                    {
                        "label": self.sanitize_clip_label(override, "Intermission"),
                        "fileName": None,
                        "url": None,
                        "absolutePath": None,
                        "sizeBytes": 0,
                    }
                )
            else:
                student_index += 1
                file_name = f"{session['id']}-clip-{index + 1}.mp4"
                output_path = session_clip_dir / file_name
                await self.crop_video_segment(
                    input_path=video_path,
                    start_seconds=start,
                    end_seconds=end,
                    output_path=output_path,
                )
                stats = output_path.stat()
                clip.update(
                    {
                        "label": self.sanitize_clip_label(override, f"Student {student_index}"),
                        "fileName": file_name,
                        "url": f"/media/clips/{session['id']}/{file_name}",
                        "absolutePath": str(output_path),
                        "sizeBytes": stats.st_size,
                    }
                )
            clips.append(clip)
        session.setdefault("outputs", {})["videoClips"] = clips
        return clips

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

    async def run_whisperx_transcription(self, session: dict[str, Any], audio_info: dict[str, Any]) -> dict[str, Path | None]:
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
        transcription_input_path = await self._prepare_whisperx_audio(audio_info)
        hf_token = self.auth.runtime.whisperx_hf_token
        whisperx_device = await self._resolve_whisperx_device(str(session["id"]))
        whisperx_compute_type = self._resolve_whisperx_compute_type(whisperx_device)
        args = [
            str(transcription_input_path),
            "--model",
            self.settings.whisperx_model,
            "--device",
            whisperx_device,
            "--compute_type",
            whisperx_compute_type,
            "--batch_size",
            str(self.settings.whisperx_batch_size),
            "--diarize",
            "--hf_token",
            hf_token,
            "--language",
            self.settings.whisperx_language,
            "--output_dir",
            str(output_dir),
            "--output_format",
            self.settings.whisperx_output_format,
        ]
        corpus_terms = (session.get("corpus") or {}).get("terms") or []
        hotwords = self.build_hotwords(corpus_terms)
        if hotwords:
            args.extend(["--hotwords", hotwords])
        if self.settings.whisperx_initial_prompt:
            args.extend(["--initial_prompt", self.settings.whisperx_initial_prompt])
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
                env={"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
                on_output=lambda stream, text: self.events.publish(
                    str(session["id"]),
                    "log",
                    {"source": f"whisperx-{stream}", "message": text},
                ),
            )
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        completed_outputs = await self.find_existing_whisperx_outputs(session, audio_info)
        if not completed_outputs:
            raise RuntimeError("WhisperX completed but no JSON output file was found.")
        return completed_outputs

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

    async def _prepare_whisperx_audio(self, audio_info: dict[str, Any]) -> Path:
        """Derive the dedicated WhisperX input WAV (16 kHz mono, filtered).

        The extracted MP3 must stay untouched — the audio-professionalism scorer
        measures loudness on it, and normalizing it would corrupt that signal.
        The WAV keeps the MP3's stem so WhisperX's output artifacts keep the
        base name the artifact cache looks up.
        """
        source_path = Path(str(audio_info["absolutePath"]))
        filters = self.settings.whisperx_audio_filters
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

    def _resolve_whisperx_compute_type(self, device: str) -> str:
        requested = str(self.settings.whisperx_compute_type or "float16").strip().lower()
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
