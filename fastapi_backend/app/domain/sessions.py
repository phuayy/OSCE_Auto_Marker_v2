"""Session vocabulary shared by every service that reads or writes a session.

The status strings are a wire contract with the browser (``src/lib/processingStage.js``
keeps the matching sets), so they are declared once here and imported, never
re-spelled at a call site.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from app.core.exceptions import AppError


class SessionStatus(StrEnum):
    WAITING_FOR_UPLOAD = "waiting_for_upload"
    UPLOADING = "uploading"
    ASSEMBLING = "assembling"
    UPLOADED = "uploaded"
    QUEUED = "queued"
    PROCESSING = "processing"
    CROPPED = "cropped"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# A job (or the upload assembler) is actively writing the session. The frontend
# refuses to open these and shows the stage gauge instead.
IN_FLIGHT_STATUSES: frozenset[str] = frozenset(
    {SessionStatus.ASSEMBLING, SessionStatus.QUEUED, SessionStatus.PROCESSING}
)

# In-flight statuses that a *job row* is responsible for. ``assembling`` is
# owned by the upload assembler, which has its own startup recovery.
JOB_DRIVEN_STATUSES: frozenset[str] = frozenset({SessionStatus.QUEUED, SessionStatus.PROCESSING})

TERMINAL_STATUSES: frozenset[str] = frozenset(
    {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED}
)

# Every output slot a session document carries, in the shape a fresh session
# starts with. Kept here (not on a service) because the upload path and the
# clip path both create sessions.
OUTPUT_KEYS: tuple[str, ...] = (
    "audio",
    "whisperxJson",
    "transcript",
    "subtitle",
    "subtitleTrack",
    "audioProfessionalism",
    "communicationScores",
    "videoClips",
    "scores",
)


def empty_outputs() -> dict[str, Any]:
    return {key: None for key in OUTPUT_KEYS}


def session_video_path(session: dict[str, Any]) -> Path:
    """The session's source video on local disk, or a 400 when it is not there.

    Every clip operation and the auto-crop job start with this lookup; the
    message names the problem the user can act on (re-upload) rather than the
    ffmpeg error a phantom path would produce later.
    """
    raw = ((session.get("files") or {}).get("video") or {}).get("absolutePath")
    path = Path(str(raw or ""))
    if not raw or not path.exists():
        raise AppError("Video file is missing for this session.", status_code=400, retryable=False)
    return path


def session_clips(session: dict[str, Any]) -> list[dict[str, Any]]:
    """The clip list, or an empty list when the session has none."""
    outputs = session.get("outputs")
    clips = outputs.get("videoClips") if isinstance(outputs, dict) else None
    return clips if isinstance(clips, list) else []


def find_clip(session: dict[str, Any], clip_id: str) -> dict[str, Any]:
    """The clip with ``clip_id`` on this session, or a 404."""
    for clip in session_clips(session):
        if isinstance(clip, dict) and str(clip.get("id")) == str(clip_id):
            return clip
    raise AppError("Clip not found for this session.", status_code=404, retryable=False)


__all__ = [
    "IN_FLIGHT_STATUSES",
    "JOB_DRIVEN_STATUSES",
    "OUTPUT_KEYS",
    "SessionStatus",
    "TERMINAL_STATUSES",
    "empty_outputs",
    "find_clip",
    "session_clips",
    "session_video_path",
]
