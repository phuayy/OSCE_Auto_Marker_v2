"""Pins the portable Windows bundle ``scripts/build_portable.py`` produces.

The offline payload (python/ + uv-cache/) only builds on Windows, so these
tests build an ``--online`` bundle from stand-ins for uv, ffmpeg and dist/ and
check the layout and launchers the target PC's double-click depends on.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "build_portable.py"
_spec = importlib.util.spec_from_file_location("build_portable", _SCRIPT)
build_portable = importlib.util.module_from_spec(_spec)
sys.modules["build_portable"] = build_portable
_spec.loader.exec_module(build_portable)


@pytest.fixture
def inputs(tmp_path: Path) -> dict[str, Path]:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    uv = tmp_path / "uv.exe"
    uv.write_bytes(b"fake uv")
    ffmpeg_zip = tmp_path / "ffmpeg.zip"
    with zipfile.ZipFile(ffmpeg_zip, "w") as zf:
        zf.writestr("ffmpeg-7.1-essentials_build/bin/ffmpeg.exe", b"ffmpeg")
        zf.writestr("ffmpeg-7.1-essentials_build/bin/ffprobe.exe", b"ffprobe")
    return {"dist": dist, "uv": uv, "ffmpeg_zip": ffmpeg_zip, "out": tmp_path / "OSCE-Marker"}


def _argv(inputs: dict[str, Path], *extra: str) -> list[str]:
    return [
        "--out", str(inputs["out"]),
        "--dist", str(inputs["dist"]),
        "--uv", str(inputs["uv"]),
        "--ffmpeg-zip", str(inputs["ffmpeg_zip"]),
        "--online",
        *extra,
    ]


def test_online_bundle_layout(inputs):
    assert build_portable.main(_argv(inputs, "--zip")) == 0
    bundle = inputs["out"]
    for rel in (
        "Install.cmd", "Start.cmd", "README.txt", "bundle.json",
        "tools/uv/uv.exe", "tools/ffmpeg/bin/ffmpeg.exe", "tools/ffmpeg/bin/ffprobe.exe",
        "app/pyproject.toml", "app/uv.lock", "app/.python-version", "app/.env.example",
        "app/scripts/run_api.py", "app/dist/index.html",
    ):
        assert (bundle / rel).is_file(), rel
    # An online bundle carries no offline payload, which is how Install.cmd
    # decides to download instead.
    assert not (bundle / "uv-cache").exists()
    assert not (bundle / "python").exists()
    manifest = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))
    assert manifest["offline"] is False and manifest["groups"] == []
    assert len(manifest["commit"]) == 40
    with zipfile.ZipFile(bundle.with_suffix(".zip")) as zf:
        assert "OSCE-Marker/Start.cmd" in zf.namelist()


def test_launchers_are_crlf_and_rendered(inputs):
    assert build_portable.main(_argv(inputs, "--canary")) == 0
    install = (inputs["out"] / "Install.cmd").read_bytes()
    assert b"\r\n" in install and b"\n" not in install.replace(b"\r\n", b"")
    text = install.decode("utf-8")
    assert build_portable.SYNC_GROUP_PLACEHOLDER not in text
    assert "--frozen --no-dev --group canary" in text


def test_existing_output_needs_force(inputs):
    inputs["out"].mkdir()
    assert build_portable.main(_argv(inputs)) == 2
    assert build_portable.main(_argv(inputs, "--force")) == 0


def test_offline_build_refused_off_windows(inputs, monkeypatch):
    monkeypatch.setattr(build_portable.os, "name", "posix")
    argv = [a for a in _argv(inputs) if a != "--online"]
    assert build_portable.main(argv) == 2
    assert not inputs["out"].exists()


def test_launchers_never_ignore_project_config():
    # UV_NO_CONFIG would also drop pyproject's [tool.uv] (the CUDA torch
    # index, package = false) and install the wrong torch.
    for name in build_portable.TEMPLATES:
        assert "UV_NO_CONFIG" not in (build_portable.TEMPLATE_DIR / name).read_text(encoding="utf-8")
