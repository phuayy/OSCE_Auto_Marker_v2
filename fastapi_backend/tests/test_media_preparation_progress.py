"""Transcode progress, the conservative retry and the throttled writes."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings
from app.core.process import CommandRunner
from app.core.resources import ResourceLease
from app.domain.enums import MediaPreparationStage, MediaPreparationStatus
from app.pipeline.ffmpeg_progress import FfmpegProgress, progress_output_handler
from app.pipeline.media import MediaPipeline
from app.services.media_preparation import TranscodeProgressReporter, prepare_session_media, update_media


# --- the parser --------------------------------------------------------------


def test_parser_reads_out_time_us_and_out_time_ms() -> None:
    parser = FfmpegProgress(100)
    assert parser.feed("out_time_us=25000000\n") == 0.25
    assert parser.feed("out_time_ms=50000000") == 0.5


def test_parser_ignores_na_garbage_negative_and_partial_lines() -> None:
    parser = FfmpegProgress(100)
    for text in ("out_time_us=N/A", "out_time_us=", "out_time_us=12ab", "out_time_us", "junk", "", "out_time_us=-9223372036854775807", "frame=3"):
        assert parser.feed(text) is None


def test_parser_clamps_and_end_never_claims_completion() -> None:
    parser = FfmpegProgress(10)
    assert parser.feed("out_time_us=99000000") == 0.99
    assert parser.feed("progress=end") is None  # already at the cap
    assert FfmpegProgress(10).feed("progress=end") == 0.99


def test_parser_handles_a_multi_line_block_and_repeats() -> None:
    parser = FfmpegProgress(10)
    block = "out_time_us=5000000\nout_time_ms=5000000\nprogress=continue\n"
    assert parser.feed(block) == 0.5
    assert parser.feed(block) is None


def test_unknown_duration_means_no_readings_and_no_handler() -> None:
    assert FfmpegProgress(0).feed("out_time_us=5") is None
    assert progress_output_handler(0, lambda f: None) is None  # type: ignore[arg-type,return-value]


def test_handler_only_listens_to_stdout() -> None:
    seen: list[float] = []

    async def report(fraction: float) -> None:
        seen.append(fraction)

    handler = progress_output_handler(10, report)
    assert handler is not None

    async def go() -> None:
        await handler("stderr", "out_time_us=5000000")
        await handler("stdout", "out_time_us=5000000")

    asyncio.run(go())
    assert seen == [0.5]


# --- the throttled reporter ---------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_reporter(session: dict[str, Any], clock: Clock, writes: list[Any], interval: float = 5.0) -> TranscodeProgressReporter:
    async def commit(current: dict[str, Any], mutate: Any) -> None:
        mutate(current)
        writes.append(dict(current["files"]["video"]["media"]))

    return TranscodeProgressReporter(session, commit, interval_seconds=interval, clock=clock)


def fresh_session() -> dict[str, Any]:
    return {"id": "s", "files": {"video": {"absolutePath": "/x", "media": {"status": "running"}}}}


def test_many_readings_become_few_writes() -> None:
    clock, writes = Clock(), []
    reporter = make_reporter(fresh_session(), clock, writes)

    async def go() -> None:
        await reporter(1, 0.0)
        for step in range(1, 1001):
            clock.now = step * 0.1  # 100 s of readings, 1000 of them
            await reporter(1, step / 1000)

    asyncio.run(go())
    assert 1 < len(writes) <= 21  # the start plus at most one per 5 s
    assert writes[0]["stage"] == MediaPreparationStage.CONVERTING and writes[0]["attempts"] == 1
    progresses = [w["progress"] for w in writes]
    assert progresses == sorted(progresses) and max(progresses) <= 0.99


def test_small_moves_and_unknown_progress_are_not_written() -> None:
    clock, writes = Clock(), []
    reporter = make_reporter(fresh_session(), clock, writes)

    async def go() -> None:
        await reporter(1, None)  # unknown duration: stage only
        clock.now = 100
        await reporter(1, 0.5)
        clock.now = 200
        await reporter(1, 0.504)  # under 0.01 further

    asyncio.run(go())
    assert writes[0]["progress"] is None
    assert writes[-1]["progress"] == 0.5 and len(writes) == 2


def test_a_new_attempt_is_written_at_once_and_restarts_progress() -> None:
    clock, writes = Clock(), []
    reporter = make_reporter(fresh_session(), clock, writes)

    async def go() -> None:
        await reporter(1, 0.0)
        clock.now = 10
        await reporter(1, 0.6)
        await reporter(2, 0.0)  # no waiting for the interval

    asyncio.run(go())
    assert (writes[-1]["attempts"], writes[-1]["progress"]) == (2, 0.0)


def test_a_failing_commit_is_not_swallowed_but_leaves_the_reporter_usable() -> None:
    async def commit(_s: Any, _m: Any) -> None:
        raise RuntimeError("db")

    reporter = TranscodeProgressReporter(fresh_session(), commit, interval_seconds=0)

    async def go() -> None:
        with pytest.raises(RuntimeError):
            await reporter(1, 0.0)
        assert reporter._writing is False

    asyncio.run(go())


def test_a_rename_between_progress_writes_is_kept() -> None:
    # The mutator patches media only, so a name written meanwhile survives.
    session = fresh_session()
    update_media(progress=0.1)(session)
    session["name"] = "Renamed"
    update_media(progress=0.5)(session)
    assert session["name"] == "Renamed" and session["files"]["video"]["media"]["progress"] == 0.5


def test_progress_is_not_written_for_a_purged_video() -> None:
    session = {"id": "s", "files": {"video": {"absolutePath": None, "purgedAt": "2026-01-01"}}}
    assert update_media(progress=0.5)(session) is False
    assert "media" not in session["files"]["video"]


# --- retry + progress through prepare_session_media ---------------------------


class ScriptedRunner:
    def __init__(self, failures: int = 0, hang: bool = False) -> None:
        self.failures, self.hang = failures, hang
        self.encodes: list[list[str]] = []

    async def run(self, binary: str, args: list[str], label: str, *, on_output: Any = None, **_: Any) -> Any:
        if "ffprobe" in label:
            return SimpleNamespace(stdout=json.dumps({
                "streams": [{"codec_type": "video", "codec_name": "hevc", "pix_fmt": "yuv420p"}],
                "format": {"duration": "100"},
            }))
        if "playback" in label:
            self.encodes.append(list(args))
            Path(args[-1]).write_bytes(b"partial")
            if self.hang:
                await asyncio.sleep(3600)
            if len(self.encodes) <= self.failures:
                raise RuntimeError("ffmpeg died")
            if on_output is not None:
                await on_output("stdout", "out_time_us=50000000\n")
                await on_output("stderr", "out_time_us=90000000\n")
            return SimpleNamespace(stdout="")
        Path(args[-1]).write_bytes(b"jpg")
        return SimpleNamespace(stdout="")


def make(tmp_path: Path, runner: ScriptedRunner) -> tuple[MediaPipeline, dict[str, Any]]:
    settings = Settings(root_dir=tmp_path, backend_root=tmp_path, media_progress_write_interval_seconds=0.0)
    media = MediaPipeline(settings, runner, events=None, auth=None, gpu=ResourceLease.unbounded())  # type: ignore[arg-type]
    video = tmp_path / "source.mp4"
    video.write_bytes(b"source")
    return media, {"id": "s1", "files": {"video": {"absolutePath": str(video), "media": {"status": "queued"}}}}


async def apply(current: dict[str, Any], mutate: Any) -> None:
    mutate(current)


def partials(tmp_path: Path) -> list[Path]:
    return list(tmp_path.rglob("*.partial*"))


def test_first_attempt_success_records_progress_cleared_and_one_attempt(tmp_path: Path) -> None:
    runner = ScriptedRunner()
    media, session = make(tmp_path, runner)
    seen: list[dict[str, Any]] = []

    async def commit(current: dict[str, Any], mutate: Any) -> None:
        mutate(current)
        seen.append(dict(current["files"]["video"]["media"]))

    asyncio.run(prepare_session_media(media, session, commit))
    final = session["files"]["video"]["media"]
    assert final["status"] == MediaPreparationStatus.READY
    assert final["attempts"] == 1 and final["stage"] is None and final["progress"] is None
    assert any(w.get("progress") == 0.5 and w.get("stage") == "converting" for w in seen)
    assert not any(w.get("progress") == 0.9 for w in seen)  # stderr is not progress
    assert [w["stage"] for w in seen][:2] == ["probing", "probing"] or seen[0]["stage"] == "probing"
    assert "-progress" in runner.encodes[0] and "-fflags" not in runner.encodes[0]


def test_conservative_profile_after_a_failure(tmp_path: Path) -> None:
    runner = ScriptedRunner(failures=1)
    media, session = make(tmp_path, runner)
    asyncio.run(prepare_session_media(media, session, apply))
    final = session["files"]["video"]["media"]
    assert final["status"] == MediaPreparationStatus.READY and final["attempts"] == 2
    second = runner.encodes[1]
    assert second[second.index("-fflags") + 1] == "+genpts"
    assert "0:a:0?" in second and "-ac" in second and "1024" in second
    assert "format=yuv420p" in second[second.index("-vf") + 1]
    for args in runner.encodes:
        assert args[args.index("-force_key_frames") + 1] == "expr:gte(t,n_forced*2)"
        assert args[args.index("-movflags") + 1] == "+faststart"
    assert "playback" in session["files"]["video"] and not partials(tmp_path)


def test_both_attempts_failing_marks_failed(tmp_path: Path) -> None:
    runner = ScriptedRunner(failures=2)
    media, session = make(tmp_path, runner)
    asyncio.run(prepare_session_media(media, session, apply))
    final = session["files"]["video"]["media"]
    assert final["status"] == MediaPreparationStatus.FAILED and final["attempts"] == 2
    assert "playable copy" in final["error"] and final["stage"] is None and final["progress"] is None
    assert "playback" not in session["files"]["video"] and not partials(tmp_path)


def test_cancellation_is_not_retried_and_leaves_no_partial(tmp_path: Path) -> None:
    runner = ScriptedRunner(hang=True)
    media, session = make(tmp_path, runner)

    async def go() -> None:
        task = asyncio.create_task(prepare_session_media(media, session, apply))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if runner.encodes:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert len(runner.encodes) == 1 and not partials(tmp_path)


def test_unknown_duration_reports_stage_only(tmp_path: Path) -> None:
    runner = ScriptedRunner()
    media, session = make(tmp_path, runner)
    original = media.probe_video_stream

    async def no_duration(path: Path) -> dict[str, Any]:
        return {**await original(path), "durationSeconds": 0.0}

    media.probe_video_stream = no_duration  # type: ignore[method-assign]
    seen: list[dict[str, Any]] = []

    async def commit(current: dict[str, Any], mutate: Any) -> None:
        mutate(current)
        seen.append(dict(current["files"]["video"]["media"]))

    asyncio.run(prepare_session_media(media, session, commit))
    assert any(w.get("stage") == "converting" and w.get("progress") is None for w in seen)
    assert all(w.get("progress") is None for w in seen)


# --- real ffmpeg ---------------------------------------------------------------


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_real_transcode_reports_progress_and_publishes(tmp_path: Path) -> None:
    source = tmp_path / "in.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=6",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-c:v", "libx265", "-c:a", "aac", "-y", str(source)],
        check=True,
    )
    settings = Settings(
        root_dir=tmp_path, backend_root=tmp_path, ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe"
    )
    media = MediaPipeline(settings, CommandRunner(tmp_path), events=None, auth=None, gpu=ResourceLease.unbounded())  # type: ignore[arg-type]
    session = {"id": "s1", "files": {"video": {"absolutePath": str(source), "media": {"status": "queued"}}}}
    readings: list[tuple[int, float | None]] = []

    async def on_progress(attempt: int, fraction: float | None) -> None:
        readings.append((attempt, fraction))

    result = asyncio.run(media.ensure_playback_copy(session, duration_seconds=6.0, on_progress=on_progress))
    assert result is not None and Path(result["absolutePath"]).stat().st_size > 0
    assert readings[0] == (1, 0.0)
    assert all(attempt == 1 and 0 <= (fraction or 0) <= 0.99 for attempt, fraction in readings)
    assert len(readings) > 1
    assert not partials(tmp_path)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_real_conservative_profile_is_valid_ffmpeg(tmp_path: Path) -> None:
    from app.pipeline.media import _BrowserCopyProfile

    source = tmp_path / "in.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-c:a", "aac", "-y", str(source)],
        check=True,
    )
    settings = Settings(root_dir=tmp_path, backend_root=tmp_path, ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
    media = MediaPipeline(settings, CommandRunner(tmp_path), events=None, auth=None, gpu=ResourceLease.unbounded())  # type: ignore[arg-type]
    out = tmp_path / "out.mp4"
    asyncio.run(media._run_ffmpeg_atomic(
        out, lambda o: media._browser_copy_args(source, o, _BrowserCopyProfile.CONSERVATIVE), "Browser playback copy (ffmpeg)"
    ))
    assert out.stat().st_size > 0
