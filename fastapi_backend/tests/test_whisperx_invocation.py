from __future__ import annotations

import asyncio
import json
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


class FakeRunner:
    """Records every command; fabricates the output files each tool would write."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.calls: list[tuple[str, list[str], str]] = []

    async def run(self, command: str, args: list[str], label: str, **_kwargs: Any) -> CommandResult:
        self.calls.append((command, list(args), label))
        if command == self.settings.whisperx_bin:
            output_dir = Path(args[args.index("--output_dir") + 1])
            output_dir.mkdir(parents=True, exist_ok=True)
            base = Path(args[0]).stem
            (output_dir / f"{base}.json").write_text(json.dumps({"segments": []}), encoding="utf-8")
        elif command == self.settings.ffmpeg_bin:
            Path(args[-1]).write_bytes(b"fake-audio")
        return CommandResult(stdout="", stderr="")

    def whisperx_args(self) -> list[str]:
        return next(args for command, args, _ in self.calls if command == self.settings.whisperx_bin)


def make_media(tmp_path: Path, **setting_overrides: Any) -> tuple[MediaPipeline, FakeRunner]:
    settings = Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        whisperx_device="cpu",
        **setting_overrides,
    )
    runner = FakeRunner(settings)
    auth = SimpleNamespace(runtime=SimpleNamespace(whisperx_hf_token="hf-token"))
    media = MediaPipeline(settings, runner=runner, events=CapturingEvents(), auth=auth)
    return media, runner


def run_transcription(media: MediaPipeline, tmp_path: Path, session: dict[str, Any] | None = None) -> dict[str, Any]:
    mp3_path = tmp_path / "session-1.mp3"
    mp3_path.write_bytes(b"fake-mp3")
    audio_info = {"fileName": mp3_path.name, "absolutePath": str(mp3_path)}
    return asyncio.run(media.run_whisperx_transcription(session or {"id": "session-1"}, audio_info))


def test_whisperx_args_pass_configured_model(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path)
    outputs = run_transcription(media, tmp_path)

    args = runner.whisperx_args()
    assert args[args.index("--model") + 1] == "large-v3"
    assert outputs["jsonAbsolutePath"] is not None
