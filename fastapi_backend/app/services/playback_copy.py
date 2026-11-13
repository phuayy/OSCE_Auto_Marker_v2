"""Give a session whose source video a browser cannot decode a copy it can.

ffmpeg reads HEVC/H.265, 10-bit H.264 and ProRes without complaint, so such an
upload transcribes, splits and scores normally — and then plays as a black box
with "Media unavailable" in the workspace. ``MediaPipeline.ensure_playback_copy``
writes an H.264 copy once; this records it on ``files.video.playback``, which
the workspace plays in place of the original. Downloads and every processing
step keep reading the original.

Best effort by design: a failed transcode costs the player, never the job that
asked for it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.logging_utils import log_context
from app.pipeline.media import MediaPipeline
from app.services.session_service import SessionMutator

logger = logging.getLogger(__name__)

Commit = Callable[[dict[str, Any], SessionMutator], Awaitable[Any]]


def _set_playback(metadata: dict[str, Any]) -> SessionMutator:
    def mutate(current: dict[str, Any]) -> Any:
        video = (current.get("files") or {}).get("video")
        if not isinstance(video, dict) or not video.get("absolutePath"):
            # Purged or replaced meanwhile: nothing to attach a copy to.
            return False
        if video.get("playback") == metadata:
            return False
        video["playback"] = dict(metadata)
        return None

    return mutate


async def record_playback_copy(media: MediaPipeline, session: dict[str, Any], commit: Commit) -> None:
    session_id = str(session.get("id") or "")
    try:
        metadata = await media.ensure_playback_copy(session)
    except Exception:
        logger.warning(
            "Could not write a browser playback copy for session %s; the original still processes.",
            session_id,
            exc_info=True,
            extra=log_context(session_id, "playback_copy"),
        )
        return
    if metadata:
        await commit(session, _set_playback(metadata))
