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

While the job runs, ``media.stage`` (``MediaPreparationStage``) says what it is
doing and ``media.progress`` (a fraction 0..1) how far the transcode is; both
are cleared when it ends. ``media.attempts`` counts the transcode attempts: the
second uses a conservative profile (see ``MediaPipeline._transcode_for_browser``).

Every write is a small mutator through the session's ``commit``, so a rename
or a clip export landing meanwhile is kept. Nothing here fails the job: a
failed poster or copy costs the player, never processing, and is reported on
``media.status`` / ``media.error`` for the workspace to show.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from pathlib import Path
from typing import Any

from app.core.logging_utils import log_context
from app.core.utils import utc_now_iso
from app.domain.enums import MediaPreparationStage, MediaPreparationStatus
from app.pipeline.media import MediaPipeline
from app.services.session_service import SessionMutator

logger = logging.getLogger(__name__)

Commit = Callable[[dict[str, Any], SessionMutator], Awaitable[Any]]

# Statuses during which the job is still expected to write something; the
# browser polls the session while it sees one of these.
PENDING_MEDIA_STATUSES = frozenset({MediaPreparationStatus.QUEUED, MediaPreparationStatus.RUNNING})

CANCELLED_MESSAGE = "Conversion was cancelled."
INTERRUPTED_MESSAGE = "Interrupted by a server restart."
COPY_FAILED_MESSAGE = "The server could not make a playable copy of this video."


class MediaPreparationOutcome(str, Enum):
    """What a finished ``prepare_media`` run did, for the caller to announce.

    Only the two outcomes a person cares about are worth a notification: a copy
    that was not there before (``COPY_WRITTEN``) and one that could not be made
    (``FAILED``). A browser-playable upload (``NOT_NEEDED``) is the common case
    on an exam day and must stay silent. A cancelled run has no outcome — it
    raises.
    """

    COPY_WRITTEN = "copy_written"
    FAILED = "failed"
    NOT_NEEDED = "not_needed"
    SKIPPED = "skipped"


class TranscodeProgressReporter:
    """Writes a transcode's progress to ``files.video.media``, sparingly.

    Every write is a session write and wakes the change feed in every open tab,
    so a reading is written only when ``interval_seconds`` have passed since the
    last write *and* it moved by at least ``MIN_STEP``. A new attempt is always
    written at once (its stage, ``attempts`` and a progress back at zero). Called
    from ``CommandRunner``'s callback on the event loop, so state needs no lock;
    a reading that arrives while a write is in flight is dropped, not queued.
    """

    MIN_STEP = 0.01

    def __init__(
        self,
        session: dict[str, Any],
        commit: Commit,
        *,
        interval_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session
        self._commit = commit
        self._interval = max(0.0, float(interval_seconds))
        self._clock = clock
        self.attempts = 0
        self._written: float | None = None
        self._written_at = 0.0
        self._writing = False

    async def __call__(self, attempt: int, fraction: float | None) -> None:
        progress = None if fraction is None else round(min(max(fraction, 0.0), 0.99), 2)
        if attempt != self.attempts:
            self.attempts = attempt
            await self._write(
                progress,
                update_media(stage=MediaPreparationStage.CONVERTING, attempts=attempt, progress=progress),
            )
            return
        if progress is None or self._writing:
            return
        if self._written is not None and progress - self._written < self.MIN_STEP:
            return
        if self._clock() - self._written_at < self._interval:
            return
        await self._write(progress, update_media(progress=progress))

    async def _write(self, progress: float | None, mutator: SessionMutator) -> None:
        self._writing = True
        try:
            await self._commit(self._session, mutator)
            self._written, self._written_at = progress, self._clock()
        finally:
            self._writing = False


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


def abandon_media(message: str) -> SessionMutator:
    """End a preparation nobody is running any more: ``failed`` with ``message``,
    progress and stage cleared. A no-op unless the record still says the job is
    pending, so it can never overwrite a result that landed meanwhile."""

    def mutate(current: dict[str, Any]) -> Any:
        video = _source_video(current)
        status = ((video or {}).get("media") or {}).get("status")
        if status not in PENDING_MEDIA_STATUSES:
            return False
        return update_media(status=MediaPreparationStatus.FAILED, error=message, stage=None, progress=None)(current)

    return mutate


def mark_media_queued() -> SessionMutator:
    """What enqueueing ``prepare_media`` writes, so the workspace can say a copy
    is on its way before the job starts."""
    return update_media(status=MediaPreparationStatus.QUEUED, stage=None, progress=None, error=None)


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


async def prepare_session_media(
    media: MediaPipeline, session: dict[str, Any], commit: Commit
) -> MediaPreparationOutcome:
    """Probe, poster, playable copy — in that order, each recorded as it lands,
    so the cheap facts reach the browser long before a transcode finishes.
    Returns what the run amounted to (see ``MediaPreparationOutcome``).

    A cancelled run records ``failed`` (shielded, so the write survives the
    cancellation) before the cancellation propagates: nothing else would ever
    move a record left at ``running`` with a progress bar."""
    try:
        return await _prepare_session_media(media, session, commit)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(commit(session, abandon_media(CANCELLED_MESSAGE)))
        except Exception:
            logger.warning("Could not record the cancelled media preparation.", exc_info=True)
        raise


async def _prepare_session_media(
    media: MediaPipeline, session: dict[str, Any], commit: Commit
) -> MediaPreparationOutcome:
    session_id = str(session.get("id") or "")
    context = log_context(session_id, "prepare_media")
    video = _source_video(session)
    if video is None:
        return MediaPreparationOutcome.SKIPPED
    source_path = Path(str(video["absolutePath"]))
    # Read now: ``commit`` refreshes ``session`` in place, so later it cannot
    # say whether this run or an earlier one recorded the copy.
    had_playback = bool(video.get("playback"))

    await commit(
        session,
        update_media(
            status=MediaPreparationStatus.RUNNING,
            stage=MediaPreparationStage.PROBING,
            progress=None,
            attempts=0,
            error=None,
        ),
    )

    probe: dict[str, Any] = {}
    try:
        probe = await media.probe_video_stream(source_path)
    except Exception:
        logger.warning("Could not probe the source video of session %s.", session_id, exc_info=True, extra=context)
    playable = media.is_browser_playable(probe) if probe.get("codec") else None
    await commit(session, update_media(**_probe_facts(probe, browser_playable=playable)))

    await commit(session, update_media(stage=MediaPreparationStage.POSTER))
    try:
        poster = await media.ensure_poster(session, duration_seconds=float(probe.get("durationSeconds") or 0))
    except Exception:
        poster = None
        logger.warning("Could not grab a poster frame for session %s.", session_id, exc_info=True, extra=context)
    if poster:
        await commit(session, set_video_artifact("poster", poster))

    error: str | None = None
    reporter = TranscodeProgressReporter(
        session, commit, interval_seconds=media.settings.media_progress_write_interval_seconds
    )
    try:
        playback = await media.ensure_playback_copy(
            session,
            duration_seconds=float(probe.get("durationSeconds") or 0),
            on_progress=reporter,
        )
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
    await commit(session, update_media(status=status, error=error, stage=None, progress=None))
    if error:
        return MediaPreparationOutcome.FAILED
    if playback and not had_playback:
        return MediaPreparationOutcome.COPY_WRITTEN
    return MediaPreparationOutcome.NOT_NEEDED
