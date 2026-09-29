"""Pins app.database.db_url — the one normaliser/validator every consumer of
the configured database URL shares (OrmDatabase, Settings.resolved_database_source,
alembic/env.py, scripts/deploy_check.py, scripts/backup_database.py,
scripts/check_database.py, scripts/reset_db.py).

F-round finding: an unrecognised scheme in APP_DATABASE_URL/DATABASE_URL
("postgresql+asyncpg://", a typo, an unsupported driver) used to silently
fall through to treating the ENTIRE URL STRING as a SQLite file path — the
app booted against a fresh, empty local SQLite file with no error. Separately,
Settings.resolved_database_source/resolved_database_path had their own,
different bug: a bare filesystem path with no scheme at all (a Windows path
typed directly into APP_DATABASE_URL) raised a confusing, wrong-context
ValueError instead of being accepted as SQLite. Both are pinned here.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.database.db_url import (
    POSTGRES_SCHEMES,
    SQLITE_SCHEMES,
    SUPPORTED_SCHEMES,
    DatabaseUrlError,
    looks_like_a_url,
    normalize_database_url,
    redact_database_url,
)
from app.database.orm import OrmDatabase

REPO_ROOT = Path(__file__).resolve().parents[2]


# --- looks_like_a_url: the URL-vs-path decision -------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "relative.db",
        "relative/app.sqlite3",
        "./data/app.db",
        "../data/app.db",
        "/var/lib/osce/app.db",
        "~/app.db",
        "~/data/app.db",
        r"C:\data\osce.sqlite3",
        "C:/data/osce.sqlite3",
        r"D:\osce\data\storage\app.sqlite3",
    ],
)
def test_looks_like_a_url_is_false_for_every_path_form(raw: str) -> None:
    assert looks_like_a_url(raw) is False


@pytest.mark.parametrize(
    "raw",
    [
        "postgresql://h/db",
        "postgresql+psycopg://h/db",
        "sqlite:///relative.db",
        "sqlite+aiosqlite:///relative.db",
        "postgresql+asyncpg://h/db",
        "mysql://h/db",
        "postgres ql://h/db",  # a typo with a space: still URL-shaped
    ],
)
def test_looks_like_a_url_is_true_for_every_url_form(raw: str) -> None:
    assert looks_like_a_url(raw) is True


def test_windows_drive_letter_is_not_mistaken_for_a_url_scheme_by_urlparse() -> None:
    """The exact trap this function exists to avoid: urlparse reports scheme
    "c" for a Windows path in either slash style (a single colon-slash, not
    "://"), which would otherwise misroute it through scheme validation."""
    from urllib.parse import urlparse

    assert urlparse(r"C:\data\osce.sqlite3").scheme == "c"
    assert urlparse("C:/data/osce.sqlite3").scheme == "c"
    assert looks_like_a_url(r"C:\data\osce.sqlite3") is False
    assert looks_like_a_url("C:/data/osce.sqlite3") is False


# --- accepted forms: exact normalised output, characterised before changing -


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgres://user:pass@host/db", "postgresql+psycopg://user:pass@host/db"),
        ("postgresql://user:pass@host/db", "postgresql+psycopg://user:pass@host/db"),
        ("postgresql+psycopg://user:pass@host/db", "postgresql+psycopg://user:pass@host/db"),
        ("postgresql://user:pass@host/db?sslmode=require", "postgresql+psycopg://user:pass@host/db?sslmode=require"),
        ("sqlite:///relative/app.sqlite3", "sqlite+aiosqlite:///relative/app.sqlite3"),
        ("sqlite:////absolute/app.sqlite3", "sqlite+aiosqlite:////absolute/app.sqlite3"),
        ("sqlite+aiosqlite:///relative/app.sqlite3", "sqlite+aiosqlite:///relative/app.sqlite3"),
        ("sqlite+aiosqlite:////absolute/app.sqlite3", "sqlite+aiosqlite:////absolute/app.sqlite3"),
        ("sqlite+aiosqlite:///:memory:", "sqlite+aiosqlite:///:memory:"),
    ],
)
def test_normalize_database_url_accepted_url_forms(raw: str, expected: str) -> None:
    assert normalize_database_url(raw) == expected
    # OrmDatabase._normalize_url must agree exactly -- one parser, not two.
    assert OrmDatabase._normalize_url(raw) == expected


def test_normalize_database_url_accepts_a_path_object() -> None:
    path = Path("/some/dir/app.sqlite3")
    result = normalize_database_url(path)
    assert result.startswith("sqlite+aiosqlite:///")
    assert result.endswith("app.sqlite3")


@pytest.mark.parametrize(
    "raw",
    [
        "relative.db",
        "relative/app.sqlite3",
        "~/app.db",
        r"C:\data\osce.sqlite3",
        "C:/data/osce.sqlite3",
        "/var/lib/osce/app.db",
    ],
)
def test_normalize_database_url_path_forms_map_to_sqlite(raw: str) -> None:
    result = normalize_database_url(raw)
    assert result.startswith("sqlite+aiosqlite:///")


# --- rejected forms: scheme named, no password, supported list shown ----


@pytest.mark.parametrize(
    "raw",
    [
        "postgresql+asyncpg://user:s3cr3t@host/db",
        "postgres+asyncpg://user:s3cr3t@host/db",
        "postgresql+psycopg2://user:s3cr3t@host/db",
        "postgresql+pg8000://user:s3cr3t@host/db",
        "mysql://user:s3cr3t@host/db",
        "sqlite+pysqlite://user:s3cr3t@host/db",
        "postgres ql://user:s3cr3t@host/db",
    ],
)
def test_normalize_database_url_rejects_unsupported_schemes(raw: str) -> None:
    with pytest.raises(DatabaseUrlError) as excinfo:
        normalize_database_url(raw)
    message = str(excinfo.value)
    assert "s3cr3t" not in message, f"password leaked into error message: {message!r}"
    for scheme in SUPPORTED_SCHEMES:
        assert scheme in message


@pytest.mark.parametrize(
    ("raw", "expected_hint"),
    [
        ("postgresql+asyncpg://u:p@h/db", "postgresql+psycopg"),
        ("postgresql+psycopg2://u:p@h/db", "postgresql+psycopg"),
        ("postgresql+pg8000://u:p@h/db", "postgresql+psycopg"),
        ("sqlite+pysqlite://u:p@h/db", "sqlite+aiosqlite"),
    ],
)
def test_known_wrong_driver_points_at_the_correct_spelling(raw: str, expected_hint: str) -> None:
    with pytest.raises(DatabaseUrlError, match=rf"Use {re.escape(expected_hint)}:// instead"):
        normalize_database_url(raw)


def test_rejected_scheme_names_what_it_saw() -> None:
    with pytest.raises(DatabaseUrlError, match="mysql"):
        normalize_database_url("mysql://user:pass@host/db")


def test_empty_url_is_rejected() -> None:
    with pytest.raises(DatabaseUrlError):
        normalize_database_url("")
    with pytest.raises(DatabaseUrlError):
        normalize_database_url("   ")


def test_orm_database_normalize_url_agrees_with_the_shared_function_on_rejection() -> None:
    """OrmDatabase._normalize_url is a thin wrapper now -- confirm it still
    raises the same error type with the same message, not a different one."""
    with pytest.raises(DatabaseUrlError, match="postgresql\\+asyncpg"):
        OrmDatabase._normalize_url("postgresql+asyncpg://u:p@h/db")


# --- redact_database_url: the one copy, moved not duplicated -------------


def test_redact_database_url_hides_the_password() -> None:
    redacted = redact_database_url("postgresql+psycopg://myuser:s3cr3t@dbhost:5432/mydb")
    assert "s3cr3t" not in redacted
    assert "myuser" in redacted
    assert "***" in redacted


def test_redact_database_url_is_a_no_op_without_a_password() -> None:
    url = "postgresql+psycopg://dbhost:5432/mydb"
    assert redact_database_url(url) == url


def test_redact_database_url_falls_back_to_regex_for_a_malformed_scheme() -> None:
    """A scheme urlsplit cannot parse at all (a stray space) leaves
    parsed.username/.password/.hostname all empty, with the credential still
    sitting in parsed.path verbatim -- the exact gap the regex fallback
    closes, since a credential must never reach an error message just
    because the URL around it happens to be broken too."""
    redacted = redact_database_url("postgres ql://user:s3cr3t@host/db")
    assert "s3cr3t" not in redacted
    assert "user" in redacted


def test_check_database_reexports_the_same_redact_function() -> None:
    """scripts/check_database.py used to carry its own copy of this function;
    it must now import the one in app.database.db_url."""
    import importlib.util

    script = REPO_ROOT / "scripts" / "check_database.py"
    spec = importlib.util.spec_from_file_location("check_database_reexport_test", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert module.redact_database_url is redact_database_url


# --- Settings.resolved_database_source / resolved_database_path ---------


def _settings(**overrides):
    from app.core.config import Settings

    return Settings(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", scorer_python_bin="python", **overrides)


@pytest.mark.parametrize(
    "raw",
    [
        "relative/app.db",
        "/abs/app.db",
        r"C:\data\app.db",
        "C:/data/app.db",
        "~/app.db",
    ],
)
def test_resolved_database_source_accepts_bare_path_forms(raw: str) -> None:
    """F-round regression: a bare filesystem path with no scheme (a Windows
    path typed directly into APP_DATABASE_URL, exactly what
    OrmDatabase._normalize_url's own path branch already accepted) used to
    raise a confusing "resolved_database_path is only available for SQLite
    URLs" ValueError from this property instead of resolving to a Path."""
    settings = _settings(app_database_url=raw)
    result = settings.resolved_database_source
    assert isinstance(result, Path)


def test_resolved_database_source_accepts_the_psycopg_qualified_scheme() -> None:
    settings = _settings(app_database_url="postgresql+psycopg://user:pass@db.internal/osce")
    assert settings.resolved_database_source == "postgresql+psycopg://user:pass@db.internal/osce"


def test_resolved_database_source_accepts_the_bare_postgres_scheme() -> None:
    settings = _settings(app_database_url="postgresql://user:pass@db.internal/osce")
    assert settings.resolved_database_source == "postgresql://user:pass@db.internal/osce"


def test_resolved_database_path_distinguishes_relative_from_absolute_sqlite_urls() -> None:
    settings = _settings(app_database_url="sqlite:///relative/app.sqlite3")
    assert settings.resolved_database_path == Path("relative/app.sqlite3")

    settings = _settings(app_database_url="sqlite:////absolute/app.sqlite3")
    assert settings.resolved_database_path == Path("/absolute/app.sqlite3")


@pytest.mark.parametrize(
    "raw",
    [
        "postgresql+asyncpg://u:s3cr3t@h/db",
        "mysql://u:s3cr3t@h/db",
        "postgresql+psycopg2://u:s3cr3t@h/db",
    ],
)
def test_resolved_database_source_rejects_unsupported_schemes(raw: str) -> None:
    settings = _settings(app_database_url=raw)
    with pytest.raises(DatabaseUrlError) as excinfo:
        _ = settings.resolved_database_source
    assert "s3cr3t" not in str(excinfo.value)


def test_settings_and_ormdatabase_agree_on_every_supported_scheme() -> None:
    """The invariant the whole fix rests on: Settings and OrmDatabase can
    never disagree about whether a scheme is supported."""
    for scheme in SUPPORTED_SCHEMES:
        raw = f"{scheme}://user:pass@host/db" if scheme in POSTGRES_SCHEMES else f"{scheme}:///relative/app.db"
        settings = _settings(app_database_url=raw)
        # Must not raise for either.
        source = settings.resolved_database_source
        if isinstance(source, str):
            OrmDatabase._normalize_url(source)
        else:
            OrmDatabase._normalize_url(source)


# --- startup: the container must refuse to build with a bad URL ---------


def test_create_container_refuses_a_bad_database_url(tmp_path: Path) -> None:
    """Where the startup check lives: OrmDatabase's constructor (via
    Settings.resolved_database_source) is called as literally the second
    line of create_container(), before any other service is wired and before
    any SQLAlchemy engine touches the network/disk -- so a bad URL is fatal
    at container construction, in every environment, for both the API
    process (main.py's lifespan calls create_container() outside its own
    try/finally) and the Hatchet worker (hatchet_worker.py calls it the same
    way). This is deliberately NOT in Settings.startup_fatal_errors(): that
    mechanism is production-only, and an invalid database URL must refuse to
    boot everywhere, not just when ENVIRONMENT=production.
    """
    from app.core.config import Settings
    from app.services.container import create_container

    settings = Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        app_database_url="postgresql+asyncpg://user:s3cr3t@host/db",
        database_url="",
    )
    with pytest.raises(DatabaseUrlError) as excinfo:
        create_container(settings)
    assert "s3cr3t" not in str(excinfo.value)
    assert "postgresql+asyncpg" in str(excinfo.value)


# --- subprocess-level: deploy_check.py / backup_database.py exit clean --


def _bad_url_env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    # Explicit, non-empty overrides for every DB-URL-shaped variable this
    # process might otherwise inherit from a real .env -- an empty value
    # does NOT override .env (app/core/config.py's load_env_file only
    # replaces a variable that reads as unset OR empty).
    env["APP_DATABASE_URL"] = "postgresql+asyncpg://user:s3cr3t@host/db"
    env["DATABASE_URL"] = "postgresql+asyncpg://user:s3cr3t@host/db"
    env["STORAGE_ROOT"] = str(tmp_path / "storage")
    return env


def test_deploy_check_exits_nonzero_with_a_clean_message_on_a_bad_url(tmp_path: Path) -> None:
    script = REPO_ROOT / "scripts" / "deploy_check.py"
    result = subprocess.run(
        [sys.executable, str(script), "--json"],
        cwd=REPO_ROOT,
        env=_bad_url_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "s3cr3t" not in result.stdout
    assert "s3cr3t" not in result.stderr
    assert "postgresql+asyncpg" in result.stdout


def test_backup_database_exits_nonzero_with_a_clean_message_on_a_bad_url(tmp_path: Path) -> None:
    script = REPO_ROOT / "scripts" / "backup_database.py"
    env = _bad_url_env(tmp_path)
    result = subprocess.run(
        [sys.executable, str(script), "--dest", str(tmp_path / "backups")],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "s3cr3t" not in result.stdout
    assert "s3cr3t" not in result.stderr
