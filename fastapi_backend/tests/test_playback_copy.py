"""Browser playback copies for source videos a browser cannot decode."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings
from app.core.resources import ResourceLease
from app.pipeline.media import MediaPipeline
from app.services.playback_copy import record_playback_copy


class FakeRunner:
    def __init__(self, stream: dict[str, str]) -> None:
        self.stream = stream
        self.calls: list[tuple[str, list[str]]] = []
        self.stdout: str | None = None

    async def run(self, binary: str, args: list[str], label: str, **_: Any) -> Any:
        self.calls.append((label, list(args)))
        if "ffprobe" in label and self.stdout is not None:
            return SimpleNamespace(stdout=self.stdout)
        if "ffprobe" in label:
            return SimpleNamespace(stdout=json.dumps({"streams": [{"codec_type": "video", **self.stream}]}))
        Path(args[-1]).write_bytes(b"mp4")  # the staging path is always last
        return SimpleNamespace(stdout="")


def make_media(tmp_path: Path, stream: dict[str, str]) -> tuple[MediaPipeline, FakeRunner]:
    settings = Settings(root_dir=tmp_path, backend_root=tmp_path, auto_crop_min_clip_seconds=1.0)
    runner = FakeRunner(stream)
    media = MediaPipeline(settings, runner, events=None, auth=None, gpu=ResourceLease.unbounded())  # type: ignore[arg-type]
    return media, runner


def session_with_video(tmp_path: Path) -> dict[str, Any]:
    video = tmp_path / "source.mp4"
    video.write_bytes(b"source")
    return {"id": "s1", "files": {"video": {"absolutePath": str(video), "url": "/media/source/x/source.mp4"}}}


@pytest.mark.parametrize(
    ("stream", "playable"),
    [
        ({"codec": "h264", "pixelFormat": "yuv420p"}, True),
        ({"codec": "h264", "pixelFormat": "yuv420p10le"}, False),
        ({"codec": "hevc", "pixelFormat": "yuv420p"}, False),
        ({"codec": "prores", "pixelFormat": "yuv422p10le"}, False),
        ({"codec": "vp9", "pixelFormat": "yuv420p10le"}, True),
        ({"codec": "av1", "pixelFormat": "yuv420p"}, True),
        ({"codec": "h264", "pixelFormat": "yuv420p", "audioCodec": "aac"}, True),
        ({"codec": "h264", "pixelFormat": "yuv420p", "audioCodec": "pcm_mulaw"}, False),
    ],
)
def test_browser_playability_rules(stream: dict[str, str], playable: bool) -> None:
    assert MediaPipeline.is_browser_playable(stream) is playable


def test_hevc_source_gets_one_h264_copy(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path, {"codec_name": "hevc", "pix_fmt": "yuv420p"})
    session = session_with_video(tmp_path)

    first = asyncio.run(media.ensure_playback_copy(session))
    assert first is not None
    assert first["url"] == "/media/playback/s1/s1-playback.mp4"
    assert Path(first["absolutePath"]).is_file()
    encode = [args for label, args in runner.calls if "playback" in label]
    assert len(encode) == 1 and "libx264" in encode[0] and "yuv420p" in encode[0]

    # A retried job reuses the copy instead of transcoding again.
    again = asyncio.run(media.ensure_playback_copy(session))
    assert again == first
    assert len([1 for label, _ in runner.calls if "playback" in label]) == 1


def test_the_reported_camera_file_needs_a_copy(tmp_path: Path) -> None:
    # The recording that showed "Media unavailable": HEVC Main + G.711 mu-law.
    media, runner = make_media(tmp_path, {})
    runner.stdout = json.dumps({"streams": [
        {"codec_type": "video", "codec_name": "hevc", "pix_fmt": "yuvj420p"},
        {"codec_type": "audio", "codec_name": "pcm_mulaw"},
    ]})
    assert asyncio.run(media.video_is_browser_playable(Path(session_with_video(tmp_path)["files"]["video"]["absolutePath"]))) is False


def test_h264_source_needs_no_copy(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path, {"codec_name": "h264", "pix_fmt": "yuv420p"})
    assert asyncio.run(media.ensure_playback_copy(session_with_video(tmp_path))) is None
    assert not [1 for label, _ in runner.calls if "playback" in label]


def test_clips_of_an_hevc_source_are_reencoded(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path, {"codec_name": "hevc", "pix_fmt": "yuv420p"})
    source = session_with_video(tmp_path)["files"]["video"]["absolutePath"]
    asyncio.run(media.crop_video_segment(
        input_path=Path(source), start_seconds=0, end_seconds=10, output_path=tmp_path / "clips" / "clip-1.mp4",
    ))
    labels = [label for label, _ in runner.calls]
    assert not any("stream copy" in label for label in labels)
    assert any("re-encode" in label for label in labels)


def test_clips_of_an_h264_source_are_still_stream_copied(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path, {"codec_name": "h264", "pix_fmt": "yuv420p"})
    source = session_with_video(tmp_path)["files"]["video"]["absolutePath"]
    asyncio.run(media.crop_video_segment(
        input_path=Path(source), start_seconds=0, end_seconds=10, output_path=tmp_path / "clips" / "clip-1.mp4",
    ))
    assert [label for label, _ in runner.calls if "ffmpeg" in label.lower() or "crop" in label] == [
        "Video clip crop (stream copy)"
    ]


def test_record_playback_copy_writes_the_copy_onto_the_session(tmp_path: Path) -> None:
    media, _ = make_media(tmp_path, {"codec_name": "hevc", "pix_fmt": "yuv420p"})
    session = session_with_video(tmp_path)

    async def commit(current: dict[str, Any], mutate: Any) -> dict[str, Any]:
        mutate(current)
        return current

    asyncio.run(record_playback_copy(media, session, commit))
    assert session["files"]["video"]["playback"]["url"] == "/media/playback/s1/s1-playback.mp4"


def test_a_failed_transcode_never_fails_the_job(tmp_path: Path) -> None:
    class Broken:
        async def ensure_playback_copy(self, _session: dict[str, Any]) -> None:
            raise RuntimeError("ffmpeg died")

    async def commit(*_: Any) -> None:
        raise AssertionError("nothing to record")

    asyncio.run(record_playback_copy(Broken(), {"id": "s1"}, commit))  # type: ignore[arg-type]
