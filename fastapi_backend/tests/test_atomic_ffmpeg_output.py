"""ffmpeg outputs the pipeline later adopts on resume are published atomically.

An MP3 or WAV left truncated at its final path by a killed ffmpeg used to be
adopted by ``_ensure_audio_output`` as a finished artifact, so only the first
minutes of a station were transcribed and scored with no error anywhere.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings
from app.core.resources import ResourceLease
from app.pipeline.media import MediaPipeline


class ScriptedRunner:
    """Writes ``content`` to the output (always the last arg), then behaves as told."""

    def __init__(self, *, content: bytes = b"full", fail: BaseException | None = None) -> None:
        self.content = content
        self.fail = fail
        self.outputs: list[Path] = []

    async def run(self, binary: str, args: list[str], label: str, **_: Any) -> Any:
        out = Path(args[-1])
        self.outputs.append(out)
        out.write_bytes(self.content)  # ffmpeg creates its output as soon as it starts
        if self.fail is not None:
            raise self.fail
        return SimpleNamespace(stdout="")


def make_media(tmp_path: Path, runner: ScriptedRunner) -> MediaPipeline:
    settings = Settings(root_dir=tmp_path, backend_root=tmp_path)
    settings.paths.ensure_layout()
    return MediaPipeline(settings, runner, events=None, auth=None, gpu=ResourceLease.unbounded())  # type: ignore[arg-type]


def session_for(tmp_path: Path) -> dict[str, Any]:
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    return {"id": "s1", "files": {"video": {"absolutePath": str(video)}}}


def audio_dir_files(media: MediaPipeline) -> list[str]:
    return sorted(p.name for p in media.settings.paths.output_audio_dir.iterdir())


def test_mp3_success_publishes_final_and_leaves_no_temp(tmp_path) -> None:
    media = make_media(tmp_path, ScriptedRunner())
    info = asyncio.run(media.extract_audio_to_mp3(session_for(tmp_path)))
    assert audio_dir_files(media) == ["s1.mp3"]
    assert Path(info["absolutePath"]).read_bytes() == b"full"


def test_mp3_ffmpeg_never_writes_the_final_path(tmp_path) -> None:
    runner = ScriptedRunner()
    media = make_media(tmp_path, runner)
    asyncio.run(media.extract_audio_to_mp3(session_for(tmp_path)))
    assert runner.outputs[0].name != "s1.mp3"
    assert runner.outputs[0].suffix == ".mp3"  # extension kept so ffmpeg picks the muxer


def test_mp3_ffmpeg_failure_leaves_no_final_file_and_no_temp(tmp_path) -> None:
    media = make_media(tmp_path, ScriptedRunner(content=b"trunc", fail=RuntimeError("killed")))
    with pytest.raises(RuntimeError):
        asyncio.run(media.extract_audio_to_mp3(session_for(tmp_path)))
    assert audio_dir_files(media) == []


def test_mp3_cancellation_leaves_no_final_file_and_no_temp(tmp_path) -> None:
    media = make_media(tmp_path, ScriptedRunner(content=b"trunc", fail=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(media.extract_audio_to_mp3(session_for(tmp_path)))
    assert audio_dir_files(media) == []


def test_mp3_empty_output_is_an_error_not_an_artifact(tmp_path) -> None:
    media = make_media(tmp_path, ScriptedRunner(content=b""))
    with pytest.raises(RuntimeError):
        asyncio.run(media.extract_audio_to_mp3(session_for(tmp_path)))
    assert audio_dir_files(media) == []


def test_leftover_temp_from_a_hard_kill_is_not_adopted_and_is_swept(tmp_path) -> None:
    media = make_media(tmp_path, ScriptedRunner())
    stale = media.settings.paths.output_audio_dir / ".s1.mp3.deadbeef.partial.mp3"
    stale.write_bytes(b"half")
    asyncio.run(media.extract_audio_to_mp3(session_for(tmp_path)))
    assert audio_dir_files(media) == ["s1.mp3"]
    assert (media.settings.paths.output_audio_dir / "s1.mp3").read_bytes() == b"full"


def test_stale_sweep_leaves_other_sessions_temps_alone(tmp_path) -> None:
    media = make_media(tmp_path, ScriptedRunner())
    other = media.settings.paths.output_audio_dir / ".s10.mp3.deadbeef.partial.mp3"
    other.write_bytes(b"theirs")
    asyncio.run(media.extract_audio_to_mp3(session_for(tmp_path)))
    assert other.exists()


def test_publish_goes_through_atomic_replace(tmp_path, monkeypatch) -> None:
    calls: list[tuple[Path, Path]] = []
    import app.pipeline.media as media_module

    real = media_module.atomic_replace
    monkeypatch.setattr(media_module, "atomic_replace", lambda s, d: (calls.append((s, d)), real(s, d))[1])
    media = make_media(tmp_path, ScriptedRunner())
    asyncio.run(media.extract_audio_to_mp3(session_for(tmp_path)))
    assert [d.name for _, d in calls] == ["s1.mp3"]


def test_wav_failure_and_success_are_atomic(tmp_path) -> None:
    runner = ScriptedRunner(content=b"trunc", fail=RuntimeError("killed"))
    media = make_media(tmp_path, runner)
    mp3 = media.settings.paths.output_audio_dir / "s1.mp3"
    mp3.write_bytes(b"mp3")
    info = {"absolutePath": str(mp3)}
    with pytest.raises(RuntimeError):
        asyncio.run(media.prepare_transcription_wav(info, "highpass=f=80"))
    assert audio_dir_files(media) == ["s1.mp3"]

    runner.fail = None
    runner.content = b"wav"
    wav = asyncio.run(media.prepare_transcription_wav(info, "highpass=f=80"))
    assert wav.name == "s1.wav" and wav.read_bytes() == b"wav"
    assert audio_dir_files(media) == ["s1.mp3", "s1.wav"]


def test_bell_wav_failure_leaves_no_final_file(tmp_path) -> None:
    media = make_media(tmp_path, ScriptedRunner(content=b"trunc", fail=RuntimeError("killed")))
    source = media.settings.paths.output_audio_dir / "s1.mp3"
    source.write_bytes(b"mp3")
    with pytest.raises(RuntimeError):
        asyncio.run(media.create_bell_detection_wav(source, 16000))
    assert audio_dir_files(media) == ["s1.mp3"]
