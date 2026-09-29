"""Tests for scripts/backup_database.py.

Subprocess-based for the same reason as test_deploy_check.py: the script's
job is to behave correctly under the environment a deploy script sets, and
in-process import would let this test's own config leak through.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
SCRIPT = ROOT_DIR / "scripts" / "backup_database.py"

from app.database.migration_runner import run_database_migrations  # noqa: E402


def _env(tmp_path: Path, *, db_name: str = "app.sqlite3") -> dict[str, str]:
    db_path = tmp_path / db_name
    storage_root = tmp_path / "storage"
    env = dict(os.environ)
    env["APP_DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    env["DATABASE_URL"] = ""
    env["STORAGE_ROOT"] = str(storage_root)
    # Point the "is the API serving" probe at a port nothing listens on so
    # restore tests are not accidentally blocked by an unrelated local service.
    env["API_HOST"] = "127.0.0.1"
    env["API_PORT"] = "18787"
    return env


def _db_path(env: dict[str, str]) -> Path:
    return Path(env["APP_DATABASE_URL"].removeprefix("sqlite:///"))


def _migrate(db_path: Path) -> None:
    asyncio.run(run_database_migrations(db_path))


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _insert_marker_row(db_path: Path, value: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO sessions (id, name, status, payload, created_at, updated_at) "
            "VALUES (?, ?, 'completed', '{}', datetime('now'), datetime('now'))",
            (str(uuid.uuid4()), value),
        )
        conn.commit()
    finally:
        conn.close()


def _session_names(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT name FROM sessions").fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def test_sqlite_backup_produces_valid_file_with_rows(tmp_path: Path) -> None:
    env = _env(tmp_path)
    db_path = _db_path(env)
    _migrate(db_path)
    _insert_marker_row(db_path, "alpha")

    dest = tmp_path / "backups"
    result = _run(env, "--dest", str(dest))

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["backend"] == "sqlite"
    backup_path = Path(payload["path"])
    assert backup_path.exists()
    assert payload["sizeBytes"] > 0
    assert payload["revision"]

    conn = sqlite3.connect(str(backup_path))
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok"
        names = [row[0] for row in conn.execute("SELECT name FROM sessions").fetchall()]
    finally:
        conn.close()
    assert "alpha" in names


def test_restore_without_confirm_exits_2(tmp_path: Path) -> None:
    env = _env(tmp_path)
    db_path = _db_path(env)
    _migrate(db_path)

    dest = tmp_path / "backups"
    backup_result = _run(env, "--dest", str(dest))
    backup_path = Path(json.loads(backup_result.stdout.strip().splitlines()[-1])["path"])

    result = _run(env, "--restore", str(backup_path))

    assert result.returncode == 2


def test_restore_with_confirm_and_force_roundtrips(tmp_path: Path) -> None:
    env = _env(tmp_path)
    db_path = _db_path(env)
    _migrate(db_path)
    _insert_marker_row(db_path, "original")

    dest = tmp_path / "backups"
    backup_result = _run(env, "--dest", str(dest))
    backup_path = Path(json.loads(backup_result.stdout.strip().splitlines()[-1])["path"])

    # Mutate the live database after the backup was taken.
    _insert_marker_row(db_path, "added-after-backup")
    assert set(_session_names(db_path)) == {"original", "added-after-backup"}

    result = _run(env, "--restore", str(backup_path), "--confirm", "--force")

    assert result.returncode == 0, result.stderr
    assert _session_names(db_path) == ["original"]


def test_prune_deletes_only_old_backup_files(tmp_path: Path) -> None:
    env = _env(tmp_path)
    db_path = _db_path(env)
    _migrate(db_path)

    dest = tmp_path / "backups"
    dest.mkdir(parents=True)

    old_file = dest / "osce-db-20200101T000000Z-norev.sqlite3"
    old_file.write_bytes(b"old")
    unrelated_file = dest / "not-a-backup.txt"
    unrelated_file.write_bytes(b"keep me")

    old_time = time.time() - 40 * 86400
    os.utime(old_file, (old_time, old_time))

    fresh_result = _run(env, "--dest", str(dest))
    fresh_path = Path(json.loads(fresh_result.stdout.strip().splitlines()[-1])["path"])

    result = _run(env, "--dest", str(dest), "--prune-days", "30")

    assert result.returncode == 0, result.stderr
    assert not old_file.exists()
    assert unrelated_file.exists()
    assert fresh_path.exists()


def test_libpq_url_and_password_extraction() -> None:
    sys.path.insert(0, str(ROOT_DIR / "scripts"))
    from backup_database import libpq_url_and_password

    url = "postgresql+psycopg://myuser:s3cr3t@dbhost:5432/mydb?sslmode=require"
    libpq_url, password = libpq_url_and_password(url)

    assert password == "s3cr3t"
    assert libpq_url == "postgresql://myuser@dbhost:5432/mydb?sslmode=require"
    assert "s3cr3t" not in libpq_url


def test_libpq_url_and_password_decodes_a_percent_encoded_password() -> None:
    """F8: urlsplit().password is NOT percent-decoded by Python's own
    urllib.parse (unlike, say, a browser's URL bar) -- a password containing
    a reserved URL character (here "@", encoded as %40) reached PGPASSWORD
    still percent-encoded, and PostgreSQL then authenticated against the
    literal encoded string, which is never the real password."""
    sys.path.insert(0, str(ROOT_DIR / "scripts"))
    from backup_database import libpq_url_and_password

    url = "postgresql+psycopg://myuser:p%40ss@dbhost:5432/mydb"
    _libpq_url, password = libpq_url_and_password(url)

    assert password == "p@ss"


def test_libpq_url_and_password_keeps_the_username_percent_encoded() -> None:
    """The username is the opposite case on purpose: it stays IN the libpq
    URL this function builds, and libpq's own URI parser percent-decodes
    each component itself -- decoding it here too would double-decode
    anything that legitimately contains a percent sign."""
    sys.path.insert(0, str(ROOT_DIR / "scripts"))
    from backup_database import libpq_url_and_password

    url = "postgresql+psycopg://my%40user:p%40ss@dbhost:5432/mydb"
    libpq_url, _password = libpq_url_and_password(url)

    assert "my%40user" in libpq_url
    assert "my@user" not in libpq_url


def test_libpq_url_and_password_never_puts_the_password_in_the_url() -> None:
    sys.path.insert(0, str(ROOT_DIR / "scripts"))
    from backup_database import libpq_url_and_password

    url = "postgresql+psycopg://myuser:p%40ss@dbhost:5432/mydb"
    libpq_url, password = libpq_url_and_password(url)

    assert password == "p@ss"
    assert "p@ss" not in libpq_url
    assert "p%40ss" not in libpq_url
    assert "ss@dbhost" not in libpq_url  # would indicate the raw password leaked in unencoded


def test_postgres_backup_missing_binary_exits_2(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["APP_DATABASE_URL"] = "postgresql+psycopg://user:pass@127.0.0.1:5999/nonexistent"
    env["DATABASE_URL"] = ""
    env["STORAGE_ROOT"] = str(tmp_path / "storage")
    env["PG_DUMP_BIN"] = str(tmp_path / "definitely-not-a-real-binary")

    dest = tmp_path / "backups"
    result = _run(env, "--dest", str(dest))

    assert result.returncode == 2
    assert "pg_dump" in result.stderr
