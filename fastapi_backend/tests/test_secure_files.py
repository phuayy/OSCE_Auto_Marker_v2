"""Secret files land 0600 from their first byte, and an existing broad one is
narrowed on the next startup — see app/core/secure_files.py and the
"World-readable secrets" gap it closes (CLAUDE.md "Host hardening").
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from app.core.secure_files import SECRET_FILE_MODE, harden, write_secret_text


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_write_secret_text_creates_an_owner_only_file(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX permission bits only")
    path = tmp_path / "secret.key"

    write_secret_text(path, "s3cr3t")

    assert path.read_text(encoding="utf-8") == "s3cr3t"
    assert _mode(path) == SECRET_FILE_MODE


def test_write_secret_text_narrows_an_existing_file_it_overwrites(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX permission bits only")
    path = tmp_path / "secret.key"
    path.write_text("old", encoding="utf-8")
    path.chmod(0o644)

    write_secret_text(path, "new")

    assert path.read_text(encoding="utf-8") == "new"
    assert _mode(path) == SECRET_FILE_MODE


def test_harden_narrows_a_world_readable_file(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX permission bits only")
    path = tmp_path / "secret.key"
    path.write_text("s3cr3t", encoding="utf-8")
    path.chmod(0o644)

    harden(path)

    assert _mode(path) == SECRET_FILE_MODE


def test_harden_leaves_an_already_narrow_file_alone(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX permission bits only")
    path = tmp_path / "secret.key"
    path.write_text("s3cr3t", encoding="utf-8")
    path.chmod(0o600)
    before = path.stat().st_mtime_ns

    harden(path)

    assert _mode(path) == SECRET_FILE_MODE
    assert path.stat().st_mtime_ns == before  # chmod on an already-correct file is a no-op, not a write


def test_harden_narrows_a_directory_to_the_mode_given(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX permission bits only")
    directory = tmp_path / "auth"
    directory.mkdir()
    directory.chmod(0o755)

    harden(directory, mode=0o700)

    assert _mode(directory) == 0o700


def test_harden_is_a_no_op_on_a_non_posix_host(tmp_path: Path, monkeypatch) -> None:
    """The permission bits mean nothing on Windows (NTFS ACLs are a different
    model), so the function must not raise or attempt a chmod there."""
    monkeypatch.setattr(os, "name", "nt")
    path = tmp_path / "secret.key"
    path.write_text("s3cr3t", encoding="utf-8")

    calls: list[Path] = []
    monkeypatch.setattr(Path, "chmod", lambda self, mode: calls.append(self))

    harden(path)

    assert calls == []


def test_harden_swallows_a_permission_error_rather_than_failing_startup(tmp_path: Path, monkeypatch) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX permission bits only")
    path = tmp_path / "secret.key"
    path.write_text("s3cr3t", encoding="utf-8")
    path.chmod(0o644)

    def _raise(self: Path, mode: int) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "chmod", _raise)

    harden(path)  # must not raise


# --- AuthService actually applies this on startup -----------------------


def test_auth_service_writes_the_signing_secret_and_secrets_file_owner_only(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX permission bits only")
    import asyncio

    from app.core.config import Settings
    from app.services.auth_service import AuthService

    settings = Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
    )
    service = AuthService(settings, users=None, directory=None)  # type: ignore[arg-type]

    asyncio.run(service.initialize())

    assert _mode(settings.paths.auth_dir) == 0o700
    assert _mode(settings.paths.auth_secret_path) == SECRET_FILE_MODE
    assert _mode(settings.paths.secrets_path) == SECRET_FILE_MODE


def test_auth_service_narrows_files_from_before_this_hardening_existed(tmp_path: Path) -> None:
    """The upgrade path: a deployment's existing world-readable secret.key
    must not stay that way forever just because it already existed."""
    if sys.platform == "win32":
        pytest.skip("POSIX permission bits only")
    import asyncio

    from app.core.config import Settings
    from app.services.auth_service import AuthService

    settings = Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
    )
    settings.paths.auth_dir.mkdir(parents=True, exist_ok=True)
    settings.paths.auth_dir.chmod(0o755)
    settings.paths.auth_secret_path.write_text("pre-existing-secret", encoding="utf-8")
    settings.paths.auth_secret_path.chmod(0o644)

    service = AuthService(settings, users=None, directory=None)  # type: ignore[arg-type]
    asyncio.run(service.initialize())

    assert service.runtime.auth_secret == "pre-existing-secret"  # the value is kept, only the mode changes
    assert _mode(settings.paths.auth_dir) == 0o700
    assert _mode(settings.paths.auth_secret_path) == SECRET_FILE_MODE
