from __future__ import annotations

import os
from pathlib import Path

from app.core.config import Settings


def make_settings(**overrides: object) -> Settings:
    defaults = {
        "ffmpeg_bin": "ffmpeg",
        "ffprobe_bin": "ffprobe",
        "scorer_python_bin": "python",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def test_bare_command_names_add_no_path_entries() -> None:
    # Bare names only survive resolution when they were already found on PATH,
    # so there is nothing to prepend and the child inherits PATH untouched.
    assert make_settings().media_tool_path_env() == {}


def test_resolved_directories_are_prepended_once(tmp_path: Path) -> None:
    bin_dir = tmp_path / "ffmpeg" / "bin"
    settings = make_settings(
        ffmpeg_bin=str(bin_dir / "ffmpeg.exe"),
        ffprobe_bin=str(bin_dir / "ffprobe.exe"),
    )

    entries = settings.media_tool_path_env()["PATH"].split(os.pathsep)

    # ffmpeg and ffprobe share a directory in every packaged build; listing it
    # twice would only lengthen the lookup.
    assert entries[0] == str(bin_dir)
    assert entries[1:] == os.environ.get("PATH", "").split(os.pathsep)


def test_separate_directories_keep_ffmpeg_first(tmp_path: Path) -> None:
    settings = make_settings(
        ffmpeg_bin=str(tmp_path / "a" / "ffmpeg.exe"),
        ffprobe_bin=str(tmp_path / "b" / "ffprobe.exe"),
    )

    entries = settings.media_tool_path_env()["PATH"].split(os.pathsep)

    assert entries[:2] == [str(tmp_path / "a"), str(tmp_path / "b")]


def test_subprocess_env_merges_python_flags_and_caller_overrides(tmp_path: Path) -> None:
    settings = make_settings(ffmpeg_bin=str(tmp_path / "bin" / "ffmpeg.exe"))

    env = settings.subprocess_env({"HF_TOKEN": "token", "PYTHONIOENCODING": "latin-1"})

    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["HF_TOKEN"] == "token"
    # Caller-supplied values win over the defaults they collide with.
    assert env["PYTHONIOENCODING"] == "latin-1"
    assert env["PATH"].startswith(str(tmp_path / "bin"))
