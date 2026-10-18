"""Build a portable, no-admin Windows bundle of the whole app.

Run this on a Windows x64 machine that has internet, uv, git and Node (your
own development PC). It produces one folder (and optionally one zip) that is
copied to a PC with none of those: the target only double-clicks
``Install.cmd`` then ``Start.cmd``. Nothing on the target needs administrator
rights, because nothing is installed outside the bundle folder:

  OSCE-Marker/
    Install.cmd  Start.cmd  README.txt   from deploy/portable/
    bundle.json                          what was packed, from which commit
    tools/uv/uv.exe                      the uv that built uv-cache/
    tools/ffmpeg/bin/ffmpeg.exe, ffprobe.exe
    python/                              uv-managed CPython (offline bundles)
    uv-cache/                            every locked wheel (offline bundles)
    app/                                 git HEAD + the built frontend in dist/

An offline bundle (the default) carries Python and every wheel in
``uv.lock``, so ``Install.cmd`` runs ``uv sync --offline`` and needs no
network. ``--online`` leaves those two out: a much smaller bundle whose
install downloads them instead.

The uv binary is copied from this machine rather than downloaded, because the
cache's on-disk format belongs to the uv version that wrote it.

    uv run --no-sync python scripts/build_portable.py --zip
    uv run --no-sync python scripts/build_portable.py --dist dist --online
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT_DIR / "deploy" / "portable"
TEMPLATES = ("Install.cmd", "Start.cmd", "README.txt")
SYNC_GROUP_PLACEHOLDER = "__SYNC_GROUP_ARGS__"

# gyan.dev's "release essentials" build: a static ffmpeg + ffprobe for Windows
# x64 that needs no installer. Pass --ffmpeg-zip to pin a specific download.
DEFAULT_FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"


class BundleError(RuntimeError):
    """A problem worth one line in the terminal, not a traceback."""


def sync_group_args(groups: list[str]) -> str:
    return " ".join(f"--group {group}" for group in groups)


def render_template(text: str, groups: list[str]) -> str:
    """The template with its placeholder filled, in CRLF as cmd.exe expects."""
    rendered = text.replace(SYNC_GROUP_PLACEHOLDER, sync_group_args(groups))
    # A bare "--frozen --no-dev " before the line end is harmless, but tidy it.
    rendered = "\n".join(line.rstrip() for line in rendered.splitlines())
    return rendered.replace("\n", "\r\n") + "\r\n"


def write_launchers(bundle: Path, groups: list[str]) -> None:
    for name in TEMPLATES:
        text = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
        (bundle / name).write_bytes(render_template(text, groups).encode("utf-8"))


def export_source(bundle_app: Path, commit: str = "HEAD") -> str:
    """Extract the committed tree (not the working copy) into ``bundle_app``."""
    sha = subprocess.run(
        ["git", "rev-parse", commit], cwd=ROOT_DIR, check=True, capture_output=True, text=True
    ).stdout.strip()
    archive = subprocess.run(
        ["git", "archive", "--format=zip", sha], cwd=ROOT_DIR, check=True, capture_output=True
    ).stdout
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        zf.extractall(bundle_app)
    return sha


def build_frontend(dist: Path | None) -> Path:
    if dist is not None:
        if not (dist / "index.html").is_file():
            raise BundleError(f"{dist} has no index.html; is it vite's dist/ output?")
        return dist
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm is None:
        raise BundleError("npm not found; install Node or pass --dist with a built dist/ folder.")
    subprocess.run([npm, "ci"], cwd=ROOT_DIR, check=True)
    subprocess.run([npm, "run", "build"], cwd=ROOT_DIR, check=True)
    return ROOT_DIR / "dist"


def fetch_bytes(source: str) -> bytes:
    if source.startswith(("http://", "https://")):
        print(f"Downloading {source}")
        with urllib.request.urlopen(source) as response:  # noqa: S310 - fixed/operator URL
            return response.read()
    return Path(source).read_bytes()


def install_ffmpeg(bundle: Path, source: str) -> None:
    """Copy ffmpeg.exe and ffprobe.exe from a Windows build zip into tools/ffmpeg/bin."""
    target = bundle / "tools" / "ffmpeg" / "bin"
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(fetch_bytes(source))) as zf:
        for wanted in ("ffmpeg.exe", "ffprobe.exe"):
            matches = [n for n in zf.namelist() if n.replace("\\", "/").split("/")[-1] == wanted]
            if not matches:
                raise BundleError(f"{wanted} not found in {source}")
            (target / wanted).write_bytes(zf.read(matches[0]))


def install_uv(bundle: Path, uv_path: Path) -> None:
    target = bundle / "tools" / "uv"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(uv_path, target / "uv.exe")


def populate_offline_payload(bundle: Path, uv_path: Path, groups: list[str]) -> None:
    """Fill python/ and uv-cache/ by doing one real install and throwing the venv away."""
    env = dict(os.environ)
    env.update(
        {
            "UV_PYTHON_INSTALL_DIR": str(bundle / "python"),
            "UV_CACHE_DIR": str(bundle / "uv-cache"),
            "UV_PYTHON_PREFERENCE": "only-managed",
            "UV_PROJECT_ENVIRONMENT": str(bundle / "_build-venv"),
        }
    )
    app = bundle / "app"
    python_version = (app / ".python-version").read_text(encoding="utf-8").strip()
    subprocess.run([str(uv_path), "python", "install", "--no-bin", python_version], env=env, check=True)
    group_args = [arg for group in groups for arg in ("--group", group)]
    subprocess.run(
        [str(uv_path), "sync", "--project", str(app), "--frozen", "--no-dev", *group_args],
        env=env,
        check=True,
    )
    shutil.rmtree(bundle / "_build-venv", ignore_errors=True)


def write_manifest(bundle: Path, *, commit: str, groups: list[str], offline: bool) -> dict:
    manifest = {
        "commit": commit,
        "builtAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "offline": offline,
        "groups": groups,
    }
    (bundle / "bundle.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def zip_bundle(bundle: Path) -> Path:
    archive = bundle.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                zf.write(path, Path(bundle.name) / path.relative_to(bundle))
    return archive


def build(args: argparse.Namespace) -> Path:
    offline = not args.online
    if offline and os.name != "nt":
        raise BundleError(
            "An offline bundle must be built on Windows: the cache has to hold Windows wheels. "
            "Build on a Windows PC, or pass --online."
        )
    uv_path = Path(args.uv) if args.uv else Path(shutil.which("uv") or "")
    if not uv_path.is_file():
        raise BundleError("uv not found; install it or pass --uv path\\to\\uv.exe.")

    bundle = args.out.resolve()
    if bundle.exists():
        if not args.force:
            raise BundleError(f"{bundle} already exists; pass --force to replace it.")
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)

    groups = ["canary"] if args.canary else []
    dist = build_frontend(args.dist)
    commit = export_source(bundle / "app")
    shutil.copytree(dist, bundle / "app" / "dist")
    install_uv(bundle, uv_path)
    install_ffmpeg(bundle, args.ffmpeg_zip)
    if offline:
        populate_offline_payload(bundle, uv_path, groups)
    write_launchers(bundle, groups)
    write_manifest(bundle, commit=commit, groups=groups, offline=offline)
    print(f"Bundle ready: {bundle} (commit {commit[:12]}, {'offline' if offline else 'online'} install)")
    if args.zip:
        print(f"Zip ready: {zip_bundle(bundle)}")
    return bundle


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT_DIR / "build" / "OSCE-Marker", help="Bundle folder to create.")
    parser.add_argument("--dist", type=Path, default=None, help="Use this built dist/ instead of running npm.")
    parser.add_argument("--ffmpeg-zip", default=DEFAULT_FFMPEG_URL, help="Windows ffmpeg build zip: URL or local path.")
    parser.add_argument("--uv", default=None, help="uv.exe to bundle (default: the one on PATH).")
    parser.add_argument("--online", action="store_true", help="Leave Python and wheels out; Install.cmd downloads them.")
    parser.add_argument("--canary", action="store_true", help="Include the optional Canary-Qwen engine (--group canary).")
    parser.add_argument("--zip", action="store_true", help="Also write <out>.zip.")
    parser.add_argument("--force", action="store_true", help="Replace --out if it exists.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        build(parse_args(argv))
    except BundleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"error: {' '.join(map(str, exc.cmd))} exited with {exc.returncode}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
