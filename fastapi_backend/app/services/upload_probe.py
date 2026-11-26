"""An early look at a video that is still uploading.

The browser cannot always preview what the user picked (MKV, AVI, HEVC), and
the full check — ``AsyncUploadService._validate_committed_video`` — only runs
after every byte has been sent. On the local backend the first parts are on
disk within seconds, so ffprobe over that *head* can already say "no audio
track" or "this will be converted" while the user can still change their mind.

The verdict is advisory and never blocks anything: a head ffprobe cannot read
(an MP4 whose index sits at the end of the file is the usual case) is
``inconclusive``, and the committed file is checked in full regardless.

Probing is cheap but not free, and the browser polls, so a verdict is cached
per (upload, file, head length): the head only changes when a new part extends
it, and once it reaches ``HEAD_MAX_BYTES`` it never changes again. The cache
is bounded (least recently used out) and lives in this process only — a miss
costs one ffprobe, never correctness.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.locks import KeyedLocks
from app.domain.enums import UploadProbeStatus, UploadProbeWarning
from app.pipeline.media import MediaPipeline

logger = logging.getLogger(__name__)

# How much of the file's start is handed to ffprobe: enough for the index of a
# faststart MP4 or the track headers of an MKV/AVI/TS, small enough to read and
# write in well under a second.
HEAD_MAX_BYTES = 16 * 1024 * 1024
PROBE_TIMEOUT_SECONDS = 20.0
CACHE_ENTRIES = 256


@dataclass(frozen=True)
class ProbeVerdict:
    status: UploadProbeStatus
    facts: dict[str, Any] | None = None
    warnings: tuple[UploadProbeWarning, ...] = ()


INCONCLUSIVE = ProbeVerdict(UploadProbeStatus.INCONCLUSIVE)


def facts_from_probe(probe: dict[str, Any], *, browser_playable: bool | None) -> dict[str, Any]:
    """The facts a browser is shown, from ``MediaPipeline.probe_video_stream``."""
    return {
        "durationSeconds": float(probe.get("durationSeconds") or 0) or None,
        "width": int(probe.get("width") or 0) or None,
        "height": int(probe.get("height") or 0) or None,
        "videoCodec": probe.get("codec") or None,
        "audioCodec": probe.get("audioCodec") or None,
        "audioStreams": int(probe.get("audioStreams") or 0),
        "rotation": int(probe.get("rotation") or 0),
        "browserPlayable": browser_playable,
    }


def warnings_for(facts: dict[str, Any]) -> tuple[UploadProbeWarning, ...]:
    found: list[UploadProbeWarning] = []
    if not facts["videoCodec"]:
        found.append(UploadProbeWarning.NO_VIDEO_STREAM)
    elif facts["browserPlayable"] is False:
        found.append(UploadProbeWarning.WILL_CONVERT)
    if facts["audioStreams"] == 0:
        found.append(UploadProbeWarning.NO_AUDIO_STREAM)
    elif facts["audioStreams"] > 1:
        found.append(UploadProbeWarning.MULTIPLE_AUDIO_STREAMS)
    if facts["rotation"]:
        found.append(UploadProbeWarning.ROTATED)
    return tuple(found)


def facts_from_media(media: Any) -> dict[str, Any] | None:
    """The facts of a committed upload, from the session's ``files.video.media``
    record. That record does not carry stream counts or rotation, so those are
    ``None`` rather than a guess; ``None`` altogether until the prepare job has
    probed the file."""
    if not isinstance(media, dict) or not media.get("videoCodec"):
        return None
    return {
        "durationSeconds": media.get("durationSeconds"),
        "width": media.get("width"),
        "height": media.get("height"),
        "videoCodec": media.get("videoCodec"),
        "audioCodec": media.get("audioCodec"),
        "audioStreams": None,
        "rotation": None,
        "browserPlayable": media.get("browserPlayable"),
    }


class UploadHeadProber:
    def __init__(self, media: MediaPipeline, *, cache_entries: int = CACHE_ENTRIES) -> None:
        self.media = media
        self._cache_entries = cache_entries
        self._cache: OrderedDict[tuple[str, str, int], ProbeVerdict] = OrderedDict()
        # Serialises probes of one upload so two tabs polling at once run one
        # ffprobe between them. Never the upload's part lock: a probe must not
        # make a part wait for it.
        self._locks = KeyedLocks()

    async def inspect(self, upload_id: str, file_id: str, original_name: str, head: bytes) -> ProbeVerdict:
        key = (upload_id, file_id, len(head))
        async with self._locks.hold(upload_id):
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
            verdict = await self._probe(original_name, head)
            self._cache[key] = verdict
            while len(self._cache) > self._cache_entries:
                self._cache.popitem(last=False)
            return verdict

    async def _probe(self, original_name: str, head: bytes) -> ProbeVerdict:
        extension = Path(original_name).suffix.lower()
        scratch = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="osce-probe-"))
        try:
            # Named for its extension only (already restricted to the video
            # types at initiate), never for the user's file name: ffprobe uses
            # the extension as a hint, nothing else.
            head_path = scratch / f"head{extension}"
            await asyncio.to_thread(head_path.write_bytes, head)
            probe = await self.media.probe_video_stream(head_path, timeout_seconds=PROBE_TIMEOUT_SECONDS)
        except (RuntimeError, OSError):
            logger.info("ffprobe could not read the head of an upload; leaving it to the full check.", exc_info=True)
            return INCONCLUSIVE
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        playable = self.media.is_browser_playable(probe) if probe.get("codec") else None
        facts = facts_from_probe(probe, browser_playable=playable)
        return ProbeVerdict(UploadProbeStatus.READY, facts, warnings_for(facts))
