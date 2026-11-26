"""scripts/reset_db.py resets the database the app is configured with.

Subprocess-based like test_backup_database.py: the script resolves its target
from the environment, exactly as the app does.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
SCRIPT = ROOT_DIR / "scripts" / "reset_db.py"


def _env(tmp_path: Path, database_url: str | None) -> dict[str, str]:
    env = dict(os.environ)
    env["APP_DATABASE_URL"] = database_url or ""
    env["DATABASE_URL"] = ""
    env["STORAGE_ROOT"] = str(tmp_path / "storage")
    return env


def _run(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--yes"], cwd=ROOT_DIR, env=env, capture_output=True, text=True, timeout=60
    )


def _make_sqlite(path: Path, *, sidecars: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(path).close()
    path.write_bytes(path.read_bytes() or b"x")
    if sidecars:
        for suffix in ("-wal", "-shm"):
            Path(str(path) + suffix).write_bytes(b"sidecar")


def test_configured_custom_sqlite_file_and_sidecars_are_deleted(tmp_path: Path) -> None:
    custom = tmp_path / "elsewhere" / "custom.sqlite3"
    _make_sqlite(custom, sidecars=True)
    env = _env(tmp_path, f"sqlite:///{custom.as_posix()}")
    # A file at the default location must be left alone: it is not this deployment's database.
    from_default = _default_db_path(env)
    _make_sqlite(from_default, sidecars=False)

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert not custom.exists()
    assert not Path(str(custom) + "-wal").exists()
    assert not Path(str(custom) + "-shm").exists()
    assert from_default.exists()


def _default_db_path(env: dict[str, str]) -> Path:
    code = "from app.core.config import Settings; print(Settings.load().paths.database_path)"
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT_DIR / "fastapi_backend",
        env={**env, "APP_DATABASE_URL": ""},
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(out.stdout.strip().splitlines()[-1])


def test_default_sqlite_file_and_sidecars_are_deleted_when_nothing_is_configured(tmp_path: Path) -> None:
    env = _env(tmp_path, None)
    default = _default_db_path(env)
    _make_sqlite(default, sidecars=True)

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert not default.exists()
    assert not Path(str(default) + "-wal").exists()
    assert not Path(str(default) + "-shm").exists()


def test_missing_sqlite_file_is_reported_not_an_error(tmp_path: Path) -> None:
    env = _env(tmp_path, f"sqlite:///{(tmp_path / 'nope.sqlite3').as_posix()}")
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "not found" in result.stdout


def test_postgres_is_never_mistaken_for_the_default_sqlite_file(tmp_path: Path, monkeypatch) -> None:
    """With PostgreSQL configured the default SQLite file is not the database:
    it is truncated through the Postgres path and the SQLite file is left alone."""
    import asyncio
    import importlib.util

    env = _env(tmp_path, "postgresql+psycopg://u:p@127.0.0.1:1/none")
    default = _default_db_path(env)
    _make_sqlite(default, sidecars=True)
    for key in ("APP_DATABASE_URL", "DATABASE_URL", "STORAGE_ROOT"):
        monkeypatch.setenv(key, env[key])

    spec = importlib.util.spec_from_file_location("reset_db_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    truncated: list[str] = []

    async def fake_reset_postgres(url: str) -> None:
        truncated.append(url)

    monkeypatch.setattr(module, "reset_postgres", fake_reset_postgres)
    asyncio.run(module.run(clear_all=False))

    assert truncated == ["postgresql+psycopg://u:p@127.0.0.1:1/none"]
    assert default.exists()
    assert Path(str(default) + "-wal").exists()
