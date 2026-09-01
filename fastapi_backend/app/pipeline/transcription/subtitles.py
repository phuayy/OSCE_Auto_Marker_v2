"""Subtitle rendering for engines that do not write their own.

WhisperX emits SRT and VTT itself; an engine that only returns segments (such
as Canary-Qwen) still has to feed the workspace video player and the download
links, so the router renders the same two files from the segments here.
"""
from __future__ import annotations

from typing import Any

# WhisperX writes "WEBVTT" then a blank line then the cues; keeping the same
# header means the browser track behaves identically whichever engine ran.
VTT_HEADER = "WEBVTT"


def format_srt_timestamp(seconds: float) -> str:
    """SRT wants hh:mm:ss,mmm and cannot express a negative offset."""
    total_milliseconds = max(int(round(float(seconds) * 1000)), 0)
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def build_srt_text(segments: list[dict[str, Any]]) -> str:
    """Render segments as SRT, skipping the ones with nothing to show.

    A cue whose end precedes its start is clamped to a zero-length cue rather
    than dropped: the text is still evidence a scorer may need, and players
    tolerate a zero-length cue where they choke on a reversed one.
    """
    cues: list[str] = []
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        start = float(segment.get("start") or 0.0)
        end_value = segment.get("end")
        end = float(end_value) if end_value is not None else start
        if end < start:
            end = start
        speaker = str(segment.get("speaker") or "").strip()
        body = f"[{speaker}] {text}" if speaker else text
        cues.append(
            f"{len(cues) + 1}\n{format_srt_timestamp(start)} --> {format_srt_timestamp(end)}\n{body}\n"
        )
    return "\n".join(cues)


def convert_srt_text_to_vtt_text(srt_text: str) -> str:
    """SRT to WebVTT: the comma decimal separator is the only real difference."""
    normalized = str(srt_text or "").replace("\r\n", "\n").replace("\r", "\n")
    converted = [
        line.replace(",", ".") if "-->" in line else line
        for line in normalized.split("\n")
    ]
    return f"{VTT_HEADER}\n\n" + "\n".join(converted) + "\n"
