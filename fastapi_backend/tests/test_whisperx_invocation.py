from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.core.config import Settings
from app.core.process import CommandResult
from app.pipeline.media import MediaPipeline

class CapturingEvents:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict[str, Any]]] = []

    async def publish(self, session_id: str, event_name: str, payload: dict[str, Any]) -> None:
        self.items.append((session_id, event_name, payload))


# A minimal but *usable* WhisperX artifact. It must carry real segment text:
# an empty-segment document is rejected as a failed transcription (see
# test_empty_transcript_guard.py), so using one here would mask arg assertions
# behind an EmptyTranscriptError.
TRANSCRIBED_SEGMENTS = {
    "language": "en",
    "segments": [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00", "text": "Good morning."}],
}


class FakeRunner:
    """Records every command; fabricates the output files each tool would write.

    ``whisperx_stdout`` replays lines through the caller's ``on_output`` hook
    the way the real runner streams a subprocess's output, which is how the
    progress-parsing path is exercised without launching WhisperX.
    """

    def __init__(
        self,
        settings: Settings,
        whisperx_payload: dict[str, Any] | None = None,
        whisperx_stdout: list[str] | None = None,
    ) -> None:
        self.settings = settings
        self.calls: list[tuple[str, list[str], str]] = []
        self.envs: list[dict[str, str]] = []
        self.whisperx_payload = TRANSCRIBED_SEGMENTS if whisperx_payload is None else whisperx_payload
        self.whisperx_stdout = list(whisperx_stdout or [])

    async def run(self, command: str, args: list[str], label: str, **kwargs: Any) -> CommandResult:
        self.calls.append((command, list(args), label))
        self.envs.append(dict(kwargs.get("env") or {}))
        on_output = kwargs.get("on_output")
        if on_output is not None and command == self.settings.whisperx_bin:
            for line in self.whisperx_stdout:
                result = on_output("stdout", line)
                if asyncio.iscoroutine(result):
                    await result
        if command == self.settings.whisperx_bin:
            output_dir = Path(args[args.index("--output_dir") + 1])
            output_dir.mkdir(parents=True, exist_ok=True)
            base = Path(args[0]).stem
            (output_dir / f"{base}.json").write_text(json.dumps(self.whisperx_payload), encoding="utf-8")
        elif command == self.settings.ffmpeg_bin:
            Path(args[-1]).write_bytes(b"fake-audio")
        return CommandResult(stdout="", stderr="")

    def whisperx_args(self) -> list[str]:
        return next(args for command, args, _ in self.calls if command == self.settings.whisperx_bin)

    def whisperx_env(self) -> dict[str, str]:
        index = next(
            position for position, (command, _, _) in enumerate(self.calls) if command == self.settings.whisperx_bin
        )
        return self.envs[index]


def make_media(
    tmp_path: Path,
    *,
    whisperx_stdout: list[str] | None = None,
    **setting_overrides: Any,
) -> tuple[MediaPipeline, FakeRunner]:
    settings = Settings(
        **{
            "root_dir": tmp_path,
            "backend_root": tmp_path,
            "ffmpeg_bin": "ffmpeg",
            "ffprobe_bin": "ffprobe",
            "scorer_python_bin": "python",
            "whisperx_device": "cpu",
            **setting_overrides,
        }
    )
    runner = FakeRunner(settings, whisperx_stdout=whisperx_stdout)
    auth = SimpleNamespace(runtime=SimpleNamespace(whisperx_hf_token="hf-token"))
    media = MediaPipeline(settings, runner=runner, events=CapturingEvents(), auth=auth)
    return media, runner


def run_transcription(
    media: MediaPipeline,
    tmp_path: Path,
    session: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    mp3_path = tmp_path / "session-1.mp3"
    mp3_path.write_bytes(b"fake-mp3")
    audio_info = {"fileName": mp3_path.name, "absolutePath": str(mp3_path)}
    return asyncio.run(
        media.run_whisperx_transcription(session or {"id": "session-1"}, audio_info, **kwargs)
    )


def test_whisperx_args_pass_configured_model(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path)
    outputs = run_transcription(media, tmp_path)

    args = runner.whisperx_args()
    assert args[args.index("--model") + 1] == "large-v3"
    assert outputs["jsonAbsolutePath"] is not None


def test_whisperx_reads_filtered_wav_and_mp3_stays_untouched(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path)
    run_transcription(media, tmp_path)

    ffmpeg_calls = [args for command, args, _ in runner.calls if command == "ffmpeg"]
    assert len(ffmpeg_calls) == 1
    assert ffmpeg_calls[0][ffmpeg_calls[0].index("-af") + 1] == "highpass=f=80,loudnorm"
    # Same stem as the MP3 so WhisperX's output JSON keeps the cached base name.
    assert runner.whisperx_args()[0].endswith("session-1.wav")
    assert (tmp_path / "session-1.mp3").read_bytes() == b"fake-mp3"


def test_empty_filter_chain_feeds_mp3_directly(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path, whisperx_audio_filters="")
    run_transcription(media, tmp_path)

    assert not any(command == "ffmpeg" for command, _, _ in runner.calls)
    assert runner.whisperx_args()[0].endswith("session-1.mp3")


def test_session_corpus_terms_become_hotwords(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path)
    session = {
        "id": "session-1",
        "corpus": {"id": "c1", "name": "Common Cold (URTI)", "terms": ["nasal block", "paracetamol"]},
    }
    run_transcription(media, tmp_path, session)

    args = runner.whisperx_args()
    assert args[args.index("--hotwords") + 1] == "nasal block, paracetamol"


def test_no_corpus_means_no_hotwords_flag(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path)
    run_transcription(media, tmp_path)

    assert "--hotwords" not in runner.whisperx_args()
    assert "--initial_prompt" not in runner.whisperx_args()


def test_initial_prompt_setting_is_forwarded(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path, whisperx_initial_prompt="A medical OSCE consultation.")
    run_transcription(media, tmp_path)

    args = runner.whisperx_args()
    assert args[args.index("--initial_prompt") + 1] == "A medical OSCE consultation."


def test_build_hotwords_caps_prompt_budget() -> None:
    terms = [f"term-{index:03d}" for index in range(300)]
    hotwords = MediaPipeline.build_hotwords(terms)

    assert len(hotwords) <= 900
    assert hotwords.startswith("term-000")  # earlier terms win the budget
    assert MediaPipeline.build_hotwords([]) == ""
    assert MediaPipeline.build_hotwords(["  ", None]) == ""


def test_whisperx_env_carries_the_resolved_ffmpeg_directory(tmp_path: Path) -> None:
    """WhisperX shells out to a bare "ffmpeg"; PATH is the only way to reach it.

    Without this the run dies inside ``whisperx.audio.load_audio`` with
    ``FileNotFoundError: [WinError 2]`` on any host where ffmpeg lives somewhere
    the process PATH does not list (a winget package directory, for one).
    """
    ffmpeg_dir = tmp_path / "tools" / "ffmpeg" / "bin"
    ffmpeg_dir.mkdir(parents=True)
    media, runner = make_media(
        tmp_path,
        ffmpeg_bin=str(ffmpeg_dir / "ffmpeg.exe"),
        ffprobe_bin=str(ffmpeg_dir / "ffprobe.exe"),
    )
    run_transcription(media, tmp_path)

    path_entries = runner.whisperx_env()["PATH"].split(os.pathsep)
    assert path_entries[0] == str(ffmpeg_dir)
    # The inherited PATH is preserved, not replaced: the child still needs the
    # interpreter and CUDA libraries it was going to find there.
    assert path_entries[1:] == os.environ.get("PATH", "").split(os.pathsep)
