"""Tests for scripts/deploy_check.py, run as a real subprocess.

Deliberately subprocess-based rather than importing the module: the script's
whole job is to behave correctly under the environment variables a deploy
script sets (APP_DATABASE_URL, STORAGE_ROOT), and importing it in-process
would let this test's own already-configured ``app.core.config`` module leak
through instead of exercising that path.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

ROOT_DIR = Path(__file__).resolve().parents[2]
SCRIPT = ROOT_DIR / "scripts" / "deploy_check.py"

from app.database.migration_runner import run_database_migrations, to_sync_url  # noqa: E402
from app.database.orm import OrmDatabase  # noqa: E402


def _env(tmp_path: Path) -> dict[str, str]:
    import os

    db_path = tmp_path / "app.sqlite3"
    storage_root = tmp_path / "storage"
    env = dict(os.environ)
    env["APP_DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    env["DATABASE_URL"] = ""
    env["STORAGE_ROOT"] = str(storage_root)
    return env


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _migrate(db_path: Path) -> None:
    asyncio.run(run_database_migrations(db_path))


def _migrate_to(db_path: Path, revision: str) -> None:
    """Stamp/upgrade to a specific (not necessarily head) revision, so a test
    can build a database that is genuinely *behind* — the case F1's old
    ``unknown = current - heads`` misreported as unknown."""
    from alembic import command
    from alembic.config import Config

    from app.database.migration_runner import ALEMBIC_SCRIPTS

    sync_url = _sync_url(db_path)
    config = Config()
    config.set_main_option("script_location", str(ALEMBIC_SCRIPTS))
    config.set_main_option("sqlalchemy.url", sync_url.replace("%", "%%"))
    config.attributes["configure_logger"] = False
    command.upgrade(config, revision)


def _penultimate_revision() -> str:
    from alembic.script import ScriptDirectory

    from app.database.migration_runner import ALEMBIC_SCRIPTS

    script_dir = ScriptDirectory(str(ALEMBIC_SCRIPTS))
    (head,) = script_dir.get_heads()
    parent = script_dir.get_revision(head).down_revision
    assert parent is not None, "expected at least two migrations to test a not-yet-upgraded DB"
    return parent


def _sync_url(db_path: Path) -> str:
    return to_sync_url(OrmDatabase._normalize_url(db_path))


def _insert_session(db_path: Path, *, status: str) -> str:
    session_id = str(uuid.uuid4())
    engine = create_engine(_sync_url(db_path), future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO sessions (id, name, status, payload, created_at, updated_at) "
                    "VALUES (:id, :name, :status, :payload, :created_at, :updated_at)"
                ),
                {
                    "id": session_id,
                    "name": "test session",
                    "status": status,
                    "payload": json.dumps({}),
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                },
            )
    finally:
        engine.dispose()
    return session_id


def _stamped_revision(db_path: Path) -> str | None:
    engine = create_engine(_sync_url(db_path), future=True)
    try:
        with engine.connect() as connection:
            from sqlalchemy import inspect

            if not inspect(connection).has_table("alembic_version"):
                return None
            return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    finally:
        engine.dispose()


def test_fresh_unmigrated_database_is_not_up_to_date(tmp_path: Path) -> None:
    env = _env(tmp_path)
    result = _run(env, "--json")

    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["revision"]["current"] == []
    assert payload["revision"]["upToDate"] is False
    assert result.returncode == 0


def test_require_up_to_date_fails_on_fresh_database(tmp_path: Path) -> None:
    env = _env(tmp_path)
    result = _run(env, "--json", "--require-up-to-date")

    assert result.returncode == 4
    payload = json.loads(result.stdout)
    assert payload["ok"] is False


def test_migrated_database_is_up_to_date(tmp_path: Path) -> None:
    env = _env(tmp_path)
    db_path = Path(env["APP_DATABASE_URL"].removeprefix("sqlite:///"))
    _migrate(db_path)

    result = _run(env, "--json", "--require-up-to-date")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["revision"]["upToDate"] is True
    assert payload["ok"] is True


def test_in_flight_session_trips_require_drained(tmp_path: Path) -> None:
    env = _env(tmp_path)
    db_path = Path(env["APP_DATABASE_URL"].removeprefix("sqlite:///"))
    _migrate(db_path)
    session_id = _insert_session(db_path, status="processing")

    result = _run(env, "--json", "--require-drained")

    assert result.returncode == 3
    payload = json.loads(result.stdout)
    assert payload["inFlight"]["sessions"] == 1
    assert session_id in payload["inFlight"]["sessionIds"]
    assert payload["ok"] is False


def test_terminal_session_does_not_trip_require_drained(tmp_path: Path) -> None:
    env = _env(tmp_path)
    db_path = Path(env["APP_DATABASE_URL"].removeprefix("sqlite:///"))
    _migrate(db_path)
    _insert_session(db_path, status="completed")

    result = _run(env, "--json", "--require-drained")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["inFlight"]["sessions"] == 0


def test_a_database_behind_head_is_not_reported_as_unknown(tmp_path: Path) -> None:
    """F1: an older *known* revision (an upgrade simply not yet run — the
    ordinary pre-deploy state) must be reported behind, not unknown. The old
    ``unknown = current - heads`` compared against heads alone, so any
    non-head revision — including the one every fresh deploy starts a
    migration from — read as unknown and Deploy-Release.ps1's drain/abort
    gate misfired on a database that only needed `alembic upgrade head`."""
    env = _env(tmp_path)
    db_path = Path(env["APP_DATABASE_URL"].removeprefix("sqlite:///"))
    penultimate = _penultimate_revision()
    _migrate_to(db_path, penultimate)

    result = _run(env, "--json")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["revision"]["current"] == [penultimate]
    assert payload["revision"]["unknown"] == []
    assert payload["revision"]["upToDate"] is False
    assert payload["ok"] is True


def test_unknown_revision_is_reported(tmp_path: Path) -> None:
    env = _env(tmp_path)
    db_path = Path(env["APP_DATABASE_URL"].removeprefix("sqlite:///"))
    _migrate(db_path)

    engine = create_engine(_sync_url(db_path), future=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM alembic_version"))
            connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('9999_future')"))
    finally:
        engine.dispose()

    result = _run(env, "--json")

    payload = json.loads(result.stdout)
    assert payload["revision"]["unknown"] == ["9999_future"]
    assert payload["revision"]["upToDate"] is False


def test_check_never_writes_alembic_version_table(tmp_path: Path) -> None:
    env = _env(tmp_path)
    db_path = Path(env["APP_DATABASE_URL"].removeprefix("sqlite:///"))
    _migrate(db_path)

    before = _stamped_revision(db_path)
    _run(env, "--json")
    _run(env, "--json", "--require-drained", "--require-up-to-date")
    after = _stamped_revision(db_path)

    assert before == after


def test_unreachable_database_exits_2(tmp_path: Path) -> None:
    import os

    env = dict(os.environ)
    # A postgres URL with nothing listening; the connection attempt fails.
    env["APP_DATABASE_URL"] = "postgresql+psycopg://user:pass@127.0.0.1:1/nonexistent?connect_timeout=2"
    env["DATABASE_URL"] = ""
    env["STORAGE_ROOT"] = str(tmp_path / "storage")

    result = _run(env, "--json")

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "error" in payload


def test_human_readable_output_without_json(tmp_path: Path) -> None:
    env = _env(tmp_path)
    db_path = Path(env["APP_DATABASE_URL"].removeprefix("sqlite:///"))
    _migrate(db_path)

    result = _run(env)

    assert result.returncode == 0
    assert "Database backend:" in result.stdout
    assert "OK: True" in result.stdout
