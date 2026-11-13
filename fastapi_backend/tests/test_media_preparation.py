"""The prepare_media job: probe, poster frame and browser playback copy.

A source the browser cannot decode (HEVC from a camera, 10-bit H.264, G.711
audio) still transcribes and splits, so without this the workspace showed a
black box and the timeline had no duration to place separators on.
"""

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
from app.domain.enums import MediaPreparationStatus, TaskType
from app.services.job_tasks import get_task_spec, queue_mirrors_session_job, queue_owns_session_status
from app.services.media_preparation import mark_media_queued, prepare_session_media


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
            return SimpleNamespace(stdout=json.dumps({
                "streams": [{"codec_type": "video", **self.stream}],
                "format": {"duration": "4512.25"},
            }))
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


async def _apply(current: dict[str, Any], mutate: Any) -> dict[str, Any]:
    """A commit double: the mutator applied to the working dict, as
    SessionService.update would apply it to the stored row."""
    mutate(current)
    return current


def test_probe_reports_duration_and_frame_size(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path, {})
    runner.stdout = json.dumps({
        "streams": [
            {"codec_type": "video", "codec_name": "hevc", "pix_fmt": "yuvj420p", "width": 2560, "height": 1440},
            {"codec_type": "audio", "codec_name": "pcm_mulaw"},
        ],
        "format": {"duration": "3605.5"},
    })
    probe = asyncio.run(media.probe_video_stream(tmp_path / "any.mp4"))
    assert probe == {
        "codec": "hevc",
        "pixelFormat": "yuvj420p",
        "audioCodec": "pcm_mulaw",
        "width": 2560,
        "height": 1440,
        "durationSeconds": 3605.5,
    }


def test_probe_tolerates_a_container_without_a_duration(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path, {})
    runner.stdout = json.dumps({"streams": [{"codec_type": "video", "codec_name": "h264", "width": "n/a"}]})
    probe = asyncio.run(media.probe_video_stream(tmp_path / "any.mp4"))
    assert probe["durationSeconds"] == 0.0 and probe["width"] == 0


def test_prepare_media_records_facts_poster_and_copy_for_an_hevc_source(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path, {"codec_name": "hevc", "pix_fmt": "yuvj420p", "width": 2560, "height": 1440})
    session = session_with_video(tmp_path)

    asyncio.run(prepare_session_media(media, session, _apply))

    video = session["files"]["video"]
    assert video["media"]["status"] == MediaPreparationStatus.READY
    assert video["media"]["durationSeconds"] == 4512.25
    assert (video["media"]["width"], video["media"]["height"]) == (2560, 1440)
    assert video["media"]["browserPlayable"] is False
    assert video["poster"]["url"] == "/media/playback/s1/s1-poster.jpg"
    assert video["playback"]["url"] == "/media/playback/s1/s1-playback.mp4"
    poster_args = next(args for label, args in runner.calls if "Poster" in label)
    # Seek before -i (fast) and take exactly one frame, a second in.
    assert poster_args.index("-ss") < poster_args.index("-i")
    assert poster_args[poster_args.index("-ss") + 1] == "1.000"
    assert "-frames:v" in poster_args


def test_prepare_media_skips_the_copy_for_a_playable_source(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path, {"codec_name": "h264", "pix_fmt": "yuv420p"})
    session = session_with_video(tmp_path)

    asyncio.run(prepare_session_media(media, session, _apply))

    video = session["files"]["video"]
    assert video["media"]["status"] == MediaPreparationStatus.READY
    assert video["media"]["browserPlayable"] is True
    assert "poster" in video and "playback" not in video
    assert not [1 for label, _ in runner.calls if "playback" in label]


def test_a_failed_transcode_is_reported_not_raised(tmp_path: Path) -> None:
    media, _ = make_media(tmp_path, {"codec_name": "hevc", "pix_fmt": "yuv420p"})

    async def broken_copy(_session: dict[str, Any]) -> None:
        raise RuntimeError("ffmpeg died")

    media.ensure_playback_copy = broken_copy  # type: ignore[method-assign]
    session = session_with_video(tmp_path)

    asyncio.run(prepare_session_media(media, session, _apply))

    video = session["files"]["video"]
    assert video["media"]["status"] == MediaPreparationStatus.FAILED
    assert "playable copy" in video["media"]["error"]
    # What did succeed is still there for the player and the timeline.
    assert video["poster"]["url"].endswith("-poster.jpg")
    assert video["media"]["durationSeconds"] == 4512.25


def test_prepare_media_does_nothing_for_a_purged_video(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path, {"codec_name": "hevc"})
    session = {"id": "s1", "files": {"video": {"absolutePath": None, "purgedAt": "2026-01-01T00:00:00Z"}}}

    asyncio.run(prepare_session_media(media, session, _apply))

    assert runner.calls == []
    assert "media" not in session["files"]["video"]


def test_mark_media_queued_resets_an_earlier_failure() -> None:
    session = {"files": {"video": {"absolutePath": "/x.mp4", "media": {"status": "failed", "error": "boom"}}}}
    mark_media_queued()(session)
    assert session["files"]["video"]["media"]["status"] == MediaPreparationStatus.QUEUED
    assert session["files"]["video"]["media"]["error"] is None


def test_prepare_media_is_a_side_task_of_the_queue() -> None:
    # It runs beside auto_crop / process_session: the session card's status
    # and job record belong to those, not to this.
    assert get_task_spec(TaskType.PREPARE_MEDIA).name == TaskType.PREPARE_MEDIA
    assert queue_owns_session_status(TaskType.PREPARE_MEDIA) is False
    assert queue_mirrors_session_job(TaskType.PREPARE_MEDIA) is False
    assert queue_mirrors_session_job(TaskType.EXPORT_CLIPS) is True
