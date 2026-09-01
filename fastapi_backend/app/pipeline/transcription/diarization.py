"""Speaker labelling for engines that do not diarise.

The three scorers read speaker-tagged dialogue: content scoring credits the
*student's* questions, and communication scoring reads the turn structure. An
engine that returns plain text (Canary-Qwen and most non-Whisper systems) would
therefore hand the scorers a transcript in which nobody can be told apart, so
the router runs a standalone pyannote pass and merges its speaker turns onto
the engine's segments here.

The merge is a pure function so it is testable without a GPU, and the
subprocess wrapper follows the same convention as the scorers: a script in
``scripts/`` that reads audio and writes JSON.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.json_utils import extract_json_object
from app.core.process import CommandRunner
from app.services.auth_service import AuthService
from app.services.event_service import EventService

logger = logging.getLogger(__name__)

DIARIZATION_SCHEMA = "speaker-turns-v1"
UNKNOWN_SPEAKER = "SPEAKER_UNKNOWN"


def overlap_seconds(start_a: float, end_a: float, start_b: float, end_b: float) -> float:
    """Length of the intersection of two intervals; 0 when they do not touch."""
    return max(0.0, min(end_a, end_b) - max(start_a, start_b))


def assign_speakers(
    segments: list[dict[str, Any]],
    turns: list[dict[str, Any]],
) -> int:
    """Label each segment in place with the speaker it overlaps most.

    Returns the number of segments that received a label. A segment overlapping
    no turn keeps whatever it had (usually ``SPEAKER_UNKNOWN``) rather than
    borrowing its neighbour's speaker: a wrong attribution is worse for
    marking than an admitted unknown, because it silently moves a student's
    words onto the simulated patient.
    """
    usable_turns = [
        (float(turn["start"]), float(turn["end"]), str(turn["speaker"]))
        for turn in turns or []
        if isinstance(turn, dict)
        and turn.get("speaker")
        and turn.get("start") is not None
        and turn.get("end") is not None
    ]
    if not usable_turns:
        return 0

    labelled = 0
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        start = float(segment.get("start") or 0.0)
        end_value = segment.get("end")
        end = float(end_value) if end_value is not None else start
        totals: dict[str, float] = {}
        for turn_start, turn_end, speaker in usable_turns:
            overlap = overlap_seconds(start, end, turn_start, turn_end)
            if overlap > 0:
                totals[speaker] = totals.get(speaker, 0.0) + overlap
        if not totals:
            continue
        # Ties break on the label to keep the merge deterministic across runs.
        segment["speaker"] = max(sorted(totals), key=lambda speaker: totals[speaker])
        labelled += 1
    return labelled


class PyannoteDiarizer:
    """Subprocess wrapper around ``scripts/pyannote_diarize.py``."""

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

    async def run(
        self,
        session_id: str,
        audio_path: Path,
        output_path: Path,
        *,
        min_speakers: int = 0,
        max_speakers: int = 0,
    ) -> list[dict[str, Any]]:
        """Diarise one audio file and return its speaker turns."""
        script_path = self.settings.diarization_script_path
        if not script_path.exists():
            raise RuntimeError(f"Diarization script not found at {script_path}")

        args = [
            str(script_path),
            "--audio",
            str(audio_path),
            "--output",
            str(output_path),
            "--model",
            self.settings.diarization_model,
            "--device",
            self.settings.diarization_device,
        ]
        if min_speakers > 0:
            args.extend(["--min-speakers", str(min_speakers)])
        if max_speakers > 0:
            args.extend(["--max-speakers", str(max_speakers)])

        await self.events.publish(
            session_id,
            "log",
            {"source": "diarization", "message": f"{self.settings.scorer_python_bin} {' '.join(args)}"},
        )
        await self.runner.run(
            self.settings.scorer_python_bin,
            args,
            "Speaker diarization (pyannote)",
            env=self.python_env(),
            on_output=lambda stream, text: self.events.publish(
                session_id,
                "log",
                {"source": f"diarization-{stream}", "message": text},
            ),
        )
        if not output_path.exists():
            raise RuntimeError("Diarization produced no output file.")
        payload = await asyncio.to_thread(
            lambda: extract_json_object(output_path.read_text(encoding="utf-8"))
        )
        if str(payload.get("schema") or "") != DIARIZATION_SCHEMA:
            raise RuntimeError("Diarization output had an unexpected schema.")
        turns = payload.get("turns")
        return list(turns) if isinstance(turns, list) else []

    def python_env(self) -> dict[str, str]:
        # pyannote reads audio through torchcodec, which dynamically links
        # FFmpeg's shared libraries; without them on PATH it degrades to a
        # "Could not load libtorchcodec" warning and a failed decode.
        env = self.settings.subprocess_env()
        token = self.auth.runtime.whisperx_hf_token
        if token:
            # pyannote's models are gated on HuggingFace; the same token the
            # WhisperX diarisation uses authorises this standalone pass.
            env["HF_TOKEN"] = token
        return env


def write_turns_file(path: Path, turns: list[dict[str, Any]]) -> None:
    """Persist a turn list in the schema :class:`PyannoteDiarizer` expects."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema": DIARIZATION_SCHEMA, "turns": turns}, indent=2),
        encoding="utf-8",
    )
