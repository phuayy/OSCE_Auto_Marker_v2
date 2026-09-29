"""Pins the release-asset contract ``scripts/ci/build_release.py`` produces.

Another session's self-hosted-PC deploy script is the actual consumer of
``osce-marker-dist.zip`` / ``release.json`` / ``SHA256SUMS.txt`` — it is not
in this checkout to test against directly, so these tests pin the contract at
the producing end: exact manifest keys, deterministic zip bytes, index.html
at the zip root, and a SHA256SUMS.txt that verifies against the real files.

Loaded by path via importlib, the same pattern ``test_rubric_section.py`` and
``test_human_segments_logic.py`` use for a script under ``scripts/`` that is
never imported as a package.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "ci" / "build_release.py"
_spec = importlib.util.spec_from_file_location("build_release", _SCRIPT)
build_release = importlib.util.module_from_spec(_spec)
# Registered before exec: build_release.py declares a frozen dataclass, and
# the dataclass decorator looks its own module up in sys.modules while
# building __init__ — without this line that lookup returns None and
# collection fails with an AttributeError that has nothing to do with the
# test itself. It also happens to be what lets promote_release.py's own
# `from build_release import ...` (loaded next) resolve against this exact
# module object instead of re-reading the file from disk under a second name.
sys.modules[_spec.name] = build_release
_spec.loader.exec_module(build_release)

_PROMOTE_SCRIPT = REPO_ROOT / "scripts" / "ci" / "promote_release.py"
_promote_spec = importlib.util.spec_from_file_location("promote_release", _PROMOTE_SCRIPT)
promote_release = importlib.util.module_from_spec(_promote_spec)
sys.modules[_promote_spec.name] = promote_release
_promote_spec.loader.exec_module(promote_release)

VALID_SHA = "a" * 40
VALID_SHA_MIXED_CASE = "A1B2c3d4" + "e5" * 16  # 40 chars, upper+lower to check normalisation


def _make_dist(tmp_path: Path, *, extra_files: dict[str, str] | None = None) -> Path:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<!doctype html><html></html>", encoding="utf-8")
    assets = dist_dir / "assets"
    assets.mkdir()
    (assets / "main.js").write_text("console.log('hi');", encoding="utf-8")
    (assets / "main.css").write_text("body { margin: 0; }", encoding="utf-8")
    for relative, content in (extra_files or {}).items():
        path = dist_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return dist_dir


# --- build_tag -----------------------------------------------------------


def test_build_tag_uses_first_twelve_hex_chars() -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    assert build_release.build_tag(sha) == f"build-{sha[:12]}"


def test_build_tag_normalises_case() -> None:
    tag = build_release.build_tag(VALID_SHA_MIXED_CASE)
    assert tag == f"build-{VALID_SHA_MIXED_CASE.lower()[:12]}"


@pytest.mark.parametrize(
    "bad_sha",
    [
        "",
        "abc123",  # too short
        "g" * 40,  # not hex
        "a" * 41,  # too long
        "a" * 39,  # too short by one
        None,
    ],
)
def test_bad_commit_sha_is_rejected(bad_sha) -> None:
    with pytest.raises(build_release.ReleaseBuildError):
        build_release.validate_commit_sha(bad_sha)
    with pytest.raises(build_release.ReleaseBuildError):
        build_release.build_tag(bad_sha)


# --- manifest --------------------------------------------------------------


def test_manifest_has_exactly_the_specified_keys() -> None:
    manifest = build_release.build_manifest(
        build_release.ManifestInputs(
            commit=VALID_SHA,
            dist_sha256="d" * 64,
            uv_lock_sha256="e" * 64,
            alembic_heads=["0016", "0001"],
            python_version="3.12",
            workflow_run_url="https://example.invalid/run/1",
            created_at="2026-01-01T00:00:00Z",
        )
    )
    assert set(manifest.keys()) == {
        "schema",
        "commit",
        "buildTag",
        "alembicHeads",
        "uvLockSha256",
        "distSha256",
        "pythonVersion",
        "workflowRunUrl",
        "createdAt",
    }
    assert manifest["schema"] == 1
    assert manifest["commit"] == VALID_SHA
    assert manifest["buildTag"] == f"build-{VALID_SHA[:12]}"
    # Sorted regardless of the order handed in — alembicHeads is meant to be
    # compared/diffed, not to encode discovery order.
    assert manifest["alembicHeads"] == ["0001", "0016"]
    assert manifest["workflowRunUrl"] == "https://example.invalid/run/1"
    assert manifest["createdAt"] == "2026-01-01T00:00:00Z"


def test_manifest_workflow_run_url_may_be_none() -> None:
    manifest = build_release.build_manifest(
        build_release.ManifestInputs(
            commit=VALID_SHA,
            dist_sha256="d" * 64,
            uv_lock_sha256="e" * 64,
            alembic_heads=[],
            python_version="3.12",
            workflow_run_url=None,
            created_at="2026-01-01T00:00:00Z",
        )
    )
    assert manifest["workflowRunUrl"] is None


def test_manifest_alembic_heads_matches_the_real_repo_heads() -> None:
    """The manifest's ``alembicHeads`` must equal what this checkout's
    revision tree actually ends at — a hardcoded or stale value here is
    exactly the kind of drift the deploy script relies on this field to
    catch."""
    heads = build_release.get_alembic_heads()
    assert heads == sorted(heads)
    assert len(heads) >= 1

    from alembic.script import ScriptDirectory

    expected = sorted(ScriptDirectory(str(REPO_ROOT / "fastapi_backend" / "alembic")).get_heads())
    assert heads == expected


def test_build_release_source_never_imports_the_app() -> None:
    """Importing ``app`` would need a working ``Settings`` (env vars, paths);
    the whole point of reading heads via ``ScriptDirectory`` directly is that
    a release build needs none of that. Checked against the source text
    rather than ``sys.path`` / ``sys.modules`` at runtime, because this same
    pytest session imports ``app`` for every other test file — a runtime
    check here would pass or fail depending on test order, not on what
    build_release.py itself does. Every other script under scripts/ that
    *does* need the app inserts fastapi_backend/ onto sys.path first (see
    scripts/deploy_check.py); build_release.py's source should contain
    neither that nor a direct ``app`` import.
    """
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "import app" not in source
    assert "from app" not in source
    assert "sys.path.insert" not in source


# --- sums text ---------------------------------------------------------


def test_build_sums_text_format_and_order() -> None:
    text = build_release.build_sums_text({"release.json": "bb", "osce-marker-dist.zip": "aa"})
    lines = text.splitlines()
    assert lines == ["aa  osce-marker-dist.zip", "bb  release.json"]
    # Two-space separator, sha256sum's own "text mode" convention.
    assert "  " in lines[0]
    assert "*" not in text


# --- zip determinism / contents ----------------------------------------


def test_zip_places_index_html_at_the_root_with_no_dist_prefix(tmp_path: Path) -> None:
    dist_dir = _make_dist(tmp_path)
    zip_path = tmp_path / "out.zip"
    build_release.write_deterministic_zip(dist_dir, zip_path)
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
    assert "index.html" in names
    assert not any(name.startswith("dist/") for name in names)
    assert "assets/main.js" in names


def test_zip_is_byte_identical_across_builds(tmp_path: Path) -> None:
    dist_dir = _make_dist(tmp_path)
    zip_a = tmp_path / "a.zip"
    zip_b = tmp_path / "b.zip"
    build_release.write_deterministic_zip(dist_dir, zip_a)
    build_release.write_deterministic_zip(dist_dir, zip_b)
    assert zip_a.read_bytes() == zip_b.read_bytes()


def test_missing_index_html_fails_with_a_clear_error(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "assets").mkdir()
    (dist_dir / "assets" / "main.js").write_text("x", encoding="utf-8")
    with pytest.raises(build_release.ReleaseBuildError, match="index.html"):
        build_release.write_deterministic_zip(dist_dir, tmp_path / "out.zip")


# --- CLI end-to-end ------------------------------------------------------


def test_cli_end_to_end(tmp_path: Path) -> None:
    dist_dir = _make_dist(tmp_path)
    out_dir = tmp_path / "release"
    exit_code = build_release.main(
        [
            "--dist",
            str(dist_dir),
            "--out",
            str(out_dir),
            "--commit",
            VALID_SHA,
            "--run-url",
            "https://example.invalid/run/42",
            "--created-at",
            "2026-01-01T00:00:00Z",
        ]
    )
    assert exit_code == 0

    zip_path = out_dir / build_release.DIST_ZIP_NAME
    manifest_path = out_dir / build_release.MANIFEST_NAME
    sums_path = out_dir / build_release.SUMS_NAME
    assert zip_path.is_file()
    assert manifest_path.is_file()
    assert sums_path.is_file()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["commit"] == VALID_SHA
    assert manifest["buildTag"] == f"build-{VALID_SHA[:12]}"
    assert manifest["distSha256"] == build_release.sha256_of_file(zip_path)
    assert manifest["uvLockSha256"] == build_release.sha256_of_bytes((REPO_ROOT / "uv.lock").read_bytes())
    assert manifest["pythonVersion"] == (REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip()

    # SHA256SUMS.txt round-trips against the real files it names.
    sums_lines = sums_path.read_text(encoding="utf-8").splitlines()
    assert len(sums_lines) == 2
    for line in sums_lines:
        digest, _, name = line.partition("  ")
        assert build_release.sha256_of_file(out_dir / name) == digest


def test_written_text_assets_use_lf_line_endings_only(tmp_path: Path) -> None:
    """Regression guard: Path.write_text's default text mode translates "\\n"
    to the platform's own line ending, so a build made on Windows without an
    explicit ``newline="\\n"`` writes CRLF into release.json and
    SHA256SUMS.txt. For SHA256SUMS.txt specifically that is not just a
    determinism wrinkle: the trailing "\\r" becomes part of the parsed
    filename, so `sha256sum -c` fails every entry as "No such file" even
    though the checksums themselves are correct — caught by actually running
    `sha256sum -c` against a real build during manual verification of this
    script, not by any assertion, which is why one lives here now."""
    dist_dir = _make_dist(tmp_path)
    out_dir = tmp_path / "release"
    build_release.build_release(dist_dir=dist_dir, out_dir=out_dir, commit=VALID_SHA, run_url=None)
    manifest_bytes = (out_dir / build_release.MANIFEST_NAME).read_bytes()
    sums_bytes = (out_dir / build_release.SUMS_NAME).read_bytes()
    assert b"\r" not in manifest_bytes
    assert b"\r" not in sums_bytes


def test_cli_missing_index_html_exits_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "readme.txt").write_text("not a build", encoding="utf-8")
    exit_code = build_release.main(
        ["--dist", str(dist_dir), "--out", str(tmp_path / "out"), "--commit", VALID_SHA]
    )
    assert exit_code != 0
    captured = capsys.readouterr()
    assert "index.html" in captured.err


def test_cli_bad_commit_exits_nonzero(tmp_path: Path) -> None:
    dist_dir = _make_dist(tmp_path)
    exit_code = build_release.main(
        ["--dist", str(dist_dir), "--out", str(tmp_path / "out"), "--commit", "not-a-sha"]
    )
    assert exit_code != 0


def test_cli_subprocess_end_to_end(tmp_path: Path) -> None:
    """The script also has to work invoked the way CI actually invokes it:
    as a subprocess, not imported."""
    dist_dir = _make_dist(tmp_path)
    out_dir = tmp_path / "release"
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--dist",
            str(dist_dir),
            "--out",
            str(out_dir),
            "--commit",
            VALID_SHA,
            "--created-at",
            "2026-01-01T00:00:00Z",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert (out_dir / build_release.MANIFEST_NAME).is_file()


# --- promote_release.py (promote.yml's verification-before-publish step) ---


def _build_a_release(tmp_path: Path, *, commit: str = VALID_SHA) -> Path:
    """A real build_release.py output, reused as the "downloaded assets" a
    promotion verifies — promote_release.py's whole job is to check files
    exactly like these, not synthetic stand-ins."""
    dist_dir = _make_dist(tmp_path)
    out_dir = tmp_path / "assets"
    exit_code = build_release.main(
        [
            "--dist",
            str(dist_dir),
            "--out",
            str(out_dir),
            "--commit",
            commit,
            "--created-at",
            "2026-01-01T00:00:00Z",
        ]
    )
    assert exit_code == 0
    return out_dir


def test_resolve_build_tag_matches_build_release() -> None:
    assert promote_release.resolve_build_tag(VALID_SHA) == build_release.build_tag(VALID_SHA)


def test_resolve_build_tag_rejects_bad_sha() -> None:
    with pytest.raises(promote_release.PromotionError):
        promote_release.resolve_build_tag("not-a-sha")


def test_parse_sums_file_round_trips() -> None:
    text = build_release.build_sums_text({"a.txt": "aa", "b.txt": "bb"})
    assert promote_release.parse_sums_file(text) == {"a.txt": "aa", "b.txt": "bb"}


def test_parse_sums_file_rejects_malformed_lines() -> None:
    with pytest.raises(promote_release.PromotionError):
        promote_release.parse_sums_file("not-sha256sum-format\n")


def test_verify_checksums_accepts_a_real_release(tmp_path: Path) -> None:
    assets_dir = _build_a_release(tmp_path)
    entries = promote_release.verify_checksums(assets_dir)
    assert set(entries) == {build_release.DIST_ZIP_NAME, build_release.MANIFEST_NAME}


def test_verify_checksums_catches_a_tampered_asset(tmp_path: Path) -> None:
    """The exact failure this exists to catch: a downloaded asset that no
    longer matches what SHA256SUMS.txt says it should hash to — corruption in
    transit, or a release someone hand-edited after publishing."""
    assets_dir = _build_a_release(tmp_path)
    zip_path = assets_dir / build_release.DIST_ZIP_NAME
    zip_path.write_bytes(zip_path.read_bytes() + b"tampered")
    with pytest.raises(promote_release.PromotionError, match="checksum mismatch"):
        promote_release.verify_checksums(assets_dir)


def test_verify_checksums_catches_a_missing_file(tmp_path: Path) -> None:
    assets_dir = _build_a_release(tmp_path)
    (assets_dir / build_release.MANIFEST_NAME).unlink()
    with pytest.raises(promote_release.PromotionError, match="missing"):
        promote_release.verify_checksums(assets_dir)


def test_verify_manifest_commit_accepts_a_matching_commit(tmp_path: Path) -> None:
    assets_dir = _build_a_release(tmp_path, commit=VALID_SHA)
    manifest = promote_release.verify_manifest_commit(assets_dir, VALID_SHA)
    assert manifest["commit"] == VALID_SHA


def test_verify_manifest_commit_rejects_a_mismatched_commit(tmp_path: Path) -> None:
    """The exact failure this exists to catch: a v-tag placed on a commit
    whose build-<sha12> release was actually built from a different one (a
    tag moved after the fact, or a name collision on the truncated sha12)."""
    other_sha = "b" * 40
    assets_dir = _build_a_release(tmp_path, commit=VALID_SHA)
    with pytest.raises(promote_release.PromotionError, match="does not match"):
        promote_release.verify_manifest_commit(assets_dir, other_sha)


def test_verify_release_assets_runs_both_checks(tmp_path: Path) -> None:
    assets_dir = _build_a_release(tmp_path, commit=VALID_SHA)
    manifest = promote_release.verify_release_assets(assets_dir, VALID_SHA)
    assert manifest["commit"] == VALID_SHA


def test_promote_cli_end_to_end(tmp_path: Path) -> None:
    assets_dir = _build_a_release(tmp_path, commit=VALID_SHA)
    exit_code = promote_release.main(["--assets-dir", str(assets_dir), "--commit", VALID_SHA])
    assert exit_code == 0


def test_promote_cli_reports_nonzero_on_mismatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assets_dir = _build_a_release(tmp_path, commit=VALID_SHA)
    other_sha = "c" * 40
    exit_code = promote_release.main(["--assets-dir", str(assets_dir), "--commit", other_sha])
    assert exit_code != 0
    assert "does not match" in capsys.readouterr().err
