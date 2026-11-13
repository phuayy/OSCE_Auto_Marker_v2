"""Prepare a session's source video for the browser — the ``prepare_media`` job.

ffmpeg reads HEVC/H.265, 10-bit H.264, ProRes and G.711 audio without
complaint, so such an upload transcribes, splits and scores normally, and then
plays as a black box in the workspace. Hosted video services solve this the
same way: whatever arrives, the server makes one copy every browser can play,
plus a still frame to show straight away. This module is that step, run as its
own job beside ``auto_crop`` / ``process_session`` so neither waits for a
transcode (see app.services.job_tasks).

It records three things under ``files.video``:

* ``media`` — what ffprobe says (duration, frame size, codecs, whether a
  browser can play the original) and the job's own ``status``. The duration
  lets the timeline work before, or without, a playable video.
* ``poster`` — one JPEG frame, shown by the player in place of a black box.
* ``playback`` — the H.264/AAC copy, only when the original needs one.

Every write is a small mutator through the session's ``commit``, so a rename
or a clip export landing meanwhile is kept. Nothing here fails the job: a
failed poster or copy costs the player, never processing, and is reported on
``media.status`` / ``media.error`` for the workspace to show.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.core.logging_utils import log_context
from app.core.utils import utc_now_iso
from app.domain.enums import MediaPreparationStatus
from app.pipeline.media import MediaPipeline
from app.services.session_service import SessionMutator

logger = logging.getLogger(__name__)

Commit = Callable[[dict[str, Any], SessionMutator], Awaitable[Any]]

# Statuses during which the job is still expected to write something; the
# browser polls the session while it sees one of these.
PENDING_MEDIA_STATUSES = frozenset({MediaPreparationStatus.QUEUED, MediaPreparationStatus.RUNNING})

COPY_FAILED_MESSAGE = "The server could not make a playable copy of this video."


def _source_video(current: dict[str, Any]) -> dict[str, Any] | None:
    """The session's source video record, or None when there is nothing to
    prepare (no video, or one already purged by retention)."""
    video = (current.get("files") or {}).get("video")
    if not isinstance(video, dict) or not video.get("absolutePath") or video.get("purgedAt"):
        return None
    return video


def update_media(**fields: Any) -> SessionMutator:
    """Merge ``fields`` into ``files.video.media``."""

    def mutate(current: dict[str, Any]) -> Any:
        video = _source_video(current)
        if video is None:
            return False
        media = dict(video.get("media") or {})
        merged = {**media, **fields}
        if merged == media:
            return False
        merged["updatedAt"] = utc_now_iso()
        video["media"] = merged
        return None

    return mutate


def mark_media_queued() -> SessionMutator:
    """What enqueueing ``prepare_media`` writes, so the workspace can say a copy
    is on its way before the job starts."""
    return update_media(status=MediaPreparationStatus.QUEUED, error=None)


def set_video_artifact(key: str, metadata: dict[str, Any]) -> SessionMutator:
    def mutate(current: dict[str, Any]) -> Any:
        video = _source_video(current)
        if video is None or video.get(key) == metadata:
            return False
        video[key] = dict(metadata)
        return None

    return mutate


def _probe_facts(probe: dict[str, Any], *, browser_playable: bool | None) -> dict[str, Any]:
    return {
        "durationSeconds": float(probe.get("durationSeconds") or 0) or None,
        "width": int(probe.get("width") or 0) or None,
        "height": int(probe.get("height") or 0) or None,
        "videoCodec": probe.get("codec") or None,
        "pixelFormat": probe.get("pixelFormat") or None,
        "audioCodec": probe.get("audioCodec") or None,
        "browserPlayable": browser_playable,
    }


async def prepare_session_media(media: MediaPipeline, session: dict[str, Any], commit: Commit) -> None:
    """Probe, poster, playable copy — in that order, each recorded as it lands,
    so the cheap facts reach the browser long before a transcode finishes."""
    session_id = str(session.get("id") or "")
    context = log_context(session_id, "prepare_media")
    video = _source_video(session)
    if video is None:
        return
    source_path = Path(str(video["absolutePath"]))

    await commit(session, update_media(status=MediaPreparationStatus.RUNNING, error=None))

    probe: dict[str, Any] = {}
    try:
        probe = await media.probe_video_stream(source_path)
    except Exception:
        logger.warning("Could not probe the source video of session %s.", session_id, exc_info=True, extra=context)
    playable = media.is_browser_playable(probe) if probe.get("codec") else None
    await commit(session, update_media(**_probe_facts(probe, browser_playable=playable)))

    try:
        poster = await media.ensure_poster(session, duration_seconds=float(probe.get("durationSeconds") or 0))
    except Exception:
        poster = None
        logger.warning("Could not grab a poster frame for session %s.", session_id, exc_info=True, extra=context)
    if poster:
        await commit(session, set_video_artifact("poster", poster))

    error: str | None = None
    try:
        playback = await media.ensure_playback_copy(session)
    except Exception:
        playback = None
        error = COPY_FAILED_MESSAGE
        logger.warning(
            "Could not write a browser playback copy for session %s; the original still processes.",
            session_id,
            exc_info=True,
            extra=context,
        )
    if playback:
        await commit(session, set_video_artifact("playback", playback))

    status = MediaPreparationStatus.FAILED if error else MediaPreparationStatus.READY
    await commit(session, update_media(status=status, error=error))
