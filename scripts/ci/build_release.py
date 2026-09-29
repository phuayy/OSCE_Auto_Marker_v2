"""Build the three release assets the CI workflow publishes to GitHub Releases.

This is the one place that knows the release contract, because the other
half of that contract — the self-hosted-PC deploy script another session
owns under ``deploy/windows/`` — consumes exactly these three files and must
never have to guess their shape:

  osce-marker-dist.zip   The built frontend (``npm run build``'s ``dist/``),
                          zipped with ``index.html`` at the zip ROOT (no
                          ``dist/`` prefix) so the deploy script can extract
                          it straight over the served directory.
  release.json            A manifest naming the commit, the build tag, the
                          alembic head(s) this build's migrations end at, and
                          the checksums that tie the other two files to it.
  SHA256SUMS.txt           ``sha256sum``-format checksums of the two files
                          above, so the deploy script (or a human) can verify
                          a download before ever unzipping it.

Everything that can be a pure function of its inputs is one — ``build_tag``,
``build_manifest``, ``build_sums_text``, ``write_deterministic_zip`` — so
``tests/test_release_manifest.py`` can pin the contract without a subprocess.
The CLI at the bottom is a thin wrapper that parses arguments, validates them
with a message meant for a CI log, and calls those functions in order.

Determinism matters here specifically: the release job re-runs this script
only when producing a *new* build, but a developer verifying a download
locally re-zips the same ``dist/`` and expects the same bytes back — so the
zip fixes every per-entry timestamp, permission bit and "which OS made this"
flag rather than trusting whatever the host happened to have. See
``write_deterministic_zip``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# scripts/ci/build_release.py -> scripts/ci -> scripts -> repo root.
ROOT_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_DIR = ROOT_DIR / "fastapi_backend" / "alembic"
UV_LOCK_PATH = ROOT_DIR / "uv.lock"
PYTHON_VERSION_PATH = ROOT_DIR / ".python-version"

# The filenames the promote workflow, the deploy script and the structural
# test (test_ci_workflows.py) all key off — one spelling, read from here
# rather than restated at each of those call sites.
DIST_ZIP_NAME = "osce-marker-dist.zip"
MANIFEST_NAME = "release.json"
SUMS_NAME = "SHA256SUMS.txt"
ASSET_NAMES = (DIST_ZIP_NAME, MANIFEST_NAME, SUMS_NAME)

MANIFEST_SCHEMA_VERSION = 1

_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")

# ZIP's own format has no timestamps before 1980; a fixed value (rather than
# "now") is what makes two builds of the same dist/ byte-identical.
_FIXED_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)
# 0o644 (rw-r--r--), packed into the high 16 bits the way Unix-created
# archives store permissions in external_attr.
_FIXED_ZIP_EXTERNAL_ATTR = 0o644 << 16


class ReleaseBuildError(ValueError):
    """A usage error meant for a CI log or a developer's terminal, not a traceback."""


def validate_commit_sha(value: str) -> str:
    """Normalise and validate a full git commit sha.

    Rejects anything that is not exactly 40 hex characters — a short sha, a
    branch name, a tag — because ``build_tag`` slices the first 12
    characters and a short or non-hex input would silently produce a
    plausible-looking but wrong tag rather than failing loudly here.
    """
    if not _COMMIT_RE.match(value or ""):
        raise ReleaseBuildError(f"--commit must be a 40-character hex git sha, got {value!r}.")
    return value.lower()


def build_tag(commit_sha: str) -> str:
    """``build-<sha12>`` — the tag both ci.yml (on push) and promote.yml (on
    tag) use to name and later look up the same build."""
    commit_sha = validate_commit_sha(commit_sha)
    return f"build-{commit_sha[:12]}"


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_dist_files(dist_dir: Path) -> list[Path]:
    """Every file under ``dist_dir``, as paths relative to it, sorted.

    Sorting is what makes ``write_deterministic_zip`` deterministic across
    filesystems that return directory entries in different orders (NTFS and
    ext4 do not agree) — everything else about the zip can be pinned per
    entry, but the entries still have to arrive in the same sequence.
    """
    if not dist_dir.is_dir():
        raise ReleaseBuildError(f"--dist {dist_dir} is not a directory.")
    relative_paths = [path.relative_to(dist_dir) for path in dist_dir.rglob("*") if path.is_file()]
    return sorted(relative_paths, key=lambda path: path.as_posix())


def validate_dist_dir(dist_dir: Path) -> None:
    """``index.html`` must sit at the dist root — the deploy script mounts
    the unzipped tree directly as the served directory, so a build whose
    entry point is missing or nested under a subfolder would 404 forever."""
    if not (dist_dir / "index.html").is_file():
        raise ReleaseBuildError(
            f"--dist {dist_dir} has no index.html at its top level; is this a `vite build` output directory?"
        )


def write_deterministic_zip(dist_dir: Path, zip_path: Path) -> None:
    """Zip ``dist_dir``'s contents at the zip root, byte-identically on every call.

    Three things zipfile leaves to the host by default, all pinned here so
    two builds of the same ``dist/`` — on the same machine or two different
    CI runners — produce the same bytes:

    * per-entry date_time (defaults to whatever ``ZipInfo()`` is handed;
      here, a fixed pre-epoch constant rather than "now" or the file's mtime)
    * ``create_system`` (``ZipInfo.__init__`` sets this from ``sys.platform``
      — 0 on Windows, 3 elsewhere — so building on a Windows dev box and a
      Linux runner would otherwise disagree byte-for-byte)
    * ``external_attr`` (defaults to 0, i.e. no permission bits recorded;
      pinned to a plain 0644 so extraction behaves the same everywhere)
    """
    validate_dist_dir(dist_dir)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for relative_path in collect_dist_files(dist_dir):
            info = zipfile.ZipInfo(relative_path.as_posix(), date_time=_FIXED_ZIP_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix, pinned — see docstring above.
            info.external_attr = _FIXED_ZIP_EXTERNAL_ATTR
            data = (dist_dir / relative_path).read_bytes()
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)


def get_alembic_heads(alembic_dir: Path = ALEMBIC_DIR) -> list[str]:
    """The migration head(s) this checkout's revision tree ends at.

    Uses ``ScriptDirectory`` directly rather than the application's own
    ``app.database.migration_runner`` so this script never imports ``app`` —
    the release build has no database and no ``Settings`` to resolve, and
    ``ScriptDirectory`` only parses the revision files in ``versions/``; it
    never executes ``alembic/env.py`` (that only happens when a command
    actually needs a database connection, e.g. ``upgrade``), so there is no
    risk of this read-only listing picking up env.py's URL-resolution side
    effects.
    """
    from alembic.script import ScriptDirectory

    if not alembic_dir.is_dir():
        raise ReleaseBuildError(f"Alembic script directory not found: {alembic_dir}")
    return sorted(ScriptDirectory(str(alembic_dir)).get_heads())


def read_python_version(path: Path = PYTHON_VERSION_PATH) -> str:
    return path.read_text(encoding="utf-8").strip()


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ManifestInputs:
    commit: str
    dist_sha256: str
    uv_lock_sha256: str
    alembic_heads: list[str]
    python_version: str
    workflow_run_url: str | None = None
    created_at: str | None = None


def build_manifest(inputs: ManifestInputs) -> dict:
    """The exact ``release.json`` contract — key set and spelling are fixed
    because the other session's deploy script parses this file by name."""
    commit = validate_commit_sha(inputs.commit)
    return {
        "schema": MANIFEST_SCHEMA_VERSION,
        "commit": commit,
        "buildTag": build_tag(commit),
        "alembicHeads": sorted(inputs.alembic_heads),
        "uvLockSha256": inputs.uv_lock_sha256,
        "distSha256": inputs.dist_sha256,
        "pythonVersion": inputs.python_version,
        "workflowRunUrl": inputs.workflow_run_url,
        "createdAt": inputs.created_at or iso_now(),
    }


def build_sums_text(entries: dict[str, str]) -> str:
    """``sha256sum``-compatible text: ``<hex>  <filename>\\n``, two spaces
    (the "text mode" marker sha256sum itself writes, as opposed to the
    ``*filename`` binary-mode form), sorted by filename so the file diffs
    cleanly and ``sha256sum -c`` round-trips regardless of build order."""
    lines = [f"{digest}  {name}\n" for name, digest in sorted(entries.items(), key=lambda item: item[0])]
    return "".join(lines)


def build_release(
    *,
    dist_dir: Path,
    out_dir: Path,
    commit: str,
    run_url: str | None,
    created_at: str | None = None,
) -> dict:
    """Build all three assets into ``out_dir`` and return the manifest dict."""
    commit = validate_commit_sha(commit)
    validate_dist_dir(dist_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    zip_path = out_dir / DIST_ZIP_NAME
    write_deterministic_zip(dist_dir, zip_path)
    dist_sha256 = sha256_of_file(zip_path)

    if not UV_LOCK_PATH.is_file():
        raise ReleaseBuildError(f"uv.lock not found at {UV_LOCK_PATH}; run from a checkout of the repo.")
    uv_lock_sha256 = sha256_of_bytes(UV_LOCK_PATH.read_bytes())

    manifest = build_manifest(
        ManifestInputs(
            commit=commit,
            dist_sha256=dist_sha256,
            uv_lock_sha256=uv_lock_sha256,
            alembic_heads=get_alembic_heads(),
            python_version=read_python_version(),
            workflow_run_url=run_url,
            created_at=created_at,
        )
    )
    manifest_path = out_dir / MANIFEST_NAME
    # newline="\n" pins LF line endings on every platform: Path.write_text's
    # default text mode translates "\n" to the platform's own line ending, so
    # without this a build made on Windows would write CRLF into release.json
    # and SHA256SUMS.txt — a determinism break of its own (Linux CI and a
    # Windows developer's local build would disagree byte-for-byte), and,
    # for SHA256SUMS.txt specifically, an outright correctness bug: the
    # trailing "\r" becomes part of the parsed filename, so `sha256sum -c`
    # reports every entry as "No such file" even though the checksums
    # themselves (computed from the real file bytes, never text-mode) are
    # correct.
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")

    sums_text = build_sums_text(
        {
            DIST_ZIP_NAME: dist_sha256,
            MANIFEST_NAME: sha256_of_file(manifest_path),
        }
    )
    (out_dir / SUMS_NAME).write_text(sums_text, encoding="utf-8", newline="\n")

    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", required=True, type=Path, help="Path to the built frontend (vite's dist/).")
    parser.add_argument("--out", required=True, type=Path, help="Directory to write the three release assets into.")
    parser.add_argument("--commit", required=True, help="Full 40-character git commit sha this build is from.")
    parser.add_argument(
        "--run-url", default=None, help="URL of the CI run that produced this build (release.json's workflowRunUrl)."
    )
    parser.add_argument(
        "--created-at",
        default=None,
        help="Override release.json's createdAt (ISO-8601 UTC, 'Z' suffix). "
        "Mainly for reproducible test fixtures; a real build omits it and gets 'now'.",
    )
    args = parser.parse_args(argv)

    try:
        manifest = build_release(
            dist_dir=args.dist,
            out_dir=args.out,
            commit=args.commit,
            run_url=args.run_url,
            created_at=args.created_at,
        )
    except ReleaseBuildError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
