"""Cover for the resource-exhaustion and hang failures found in the audit.

Each of these is a production failure with no in-app recovery: unbounded disk
or memory growth, a job pinned forever on a hung child, or leaked database
connections. They are grouped here because they share that character rather
than a module.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.api.dependencies import client_ip as _client_ip
from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.process import CommandRunner
from app.database.orm import OrmDatabase
from sqlalchemy import text as sa_text
from app.services.storage_service import LocalObjectStorageService


def _settings(tmp_path, **overrides) -> Settings:
    defaults = {
        "root_dir": tmp_path,
        "backend_root": tmp_path,
        "ffmpeg_bin": "ffmpeg",
        "ffprobe_bin": "ffprobe",
        "scorer_python_bin": "python",
        "app_database_url": "",
        "database_url": "",
    }
    return Settings(**{**defaults, **overrides})


def _upload(declared_size: int, part_size: int) -> dict:
    return {
        "id": "upload-1",
        "status": "uploading",
        "files": [
            {
                "fileId": "file-1",
                "kind": "video",
                "key": "sessions/s-1/source/video/v.mp4",
                "sizeBytes": declared_size,
                "parts": [],
                "status": "uploading",
            }
        ],
    }


# --- upload: bounded disk growth -------------------------------------------


def test_parts_cannot_exceed_the_declared_file_size(tmp_path) -> None:
    """The disk-exhaustion vector: declare a small file, then stream gigabytes
    across distinct part numbers. Only `complete` compared totals before, by
    which point the bytes were already on disk."""

    async def scenario() -> None:
        settings = _settings(tmp_path, upload_part_size_mb=1)
        storage = LocalObjectStorageService(settings)
        upload = _upload(declared_size=10, part_size=settings.upload_part_size_bytes)

        await storage.put_part(upload, "file-1", 1, b"0123456789")  # exactly the declaration

        with pytest.raises(AppError) as caught:
            await storage.put_part(upload, "file-1", 2, b"more")
        assert caught.value.status_code == 413
        assert "declared size" in caught.value.message

    asyncio.run(scenario())


def test_replacing_a_part_does_not_count_the_old_copy(tmp_path) -> None:
    """A retried part must not be double-counted against the declaration, or a
    resumed upload would fail partway through."""

    async def scenario() -> None:
        settings = _settings(tmp_path, upload_part_size_mb=1)
        storage = LocalObjectStorageService(settings)
        upload = _upload(declared_size=10, part_size=settings.upload_part_size_bytes)

        await storage.put_part(upload, "file-1", 1, b"01234")
        await storage.put_part(upload, "file-1", 1, b"56789")  # same part, retried

        assert upload["files"][0]["uploadedBytes"] == 5

    asyncio.run(scenario())


def test_a_part_within_the_declaration_is_accepted(tmp_path) -> None:
    async def scenario() -> None:
        settings = _settings(tmp_path, upload_part_size_mb=1)
        storage = LocalObjectStorageService(settings)
        upload = _upload(declared_size=10, part_size=settings.upload_part_size_bytes)

        await storage.put_part(upload, "file-1", 1, b"01234")
        result = await storage.put_part(upload, "file-1", 2, b"56789")

        assert result["uploadedBytes"] == 10

    asyncio.run(scenario())


def test_oversized_part_is_still_rejected(tmp_path) -> None:
    async def scenario() -> None:
        settings = _settings(tmp_path, upload_part_size_mb=1)
        storage = LocalObjectStorageService(settings)
        upload = _upload(declared_size=10_000_000, part_size=settings.upload_part_size_bytes)

        with pytest.raises(AppError) as caught:
            await storage.put_part(upload, "file-1", 1, b"x" * (settings.upload_part_size_bytes + 1))
        assert caught.value.status_code == 413

    asyncio.run(scenario())


# --- subprocess watchdog ---------------------------------------------------


def test_a_hung_child_is_terminated_and_reported(tmp_path) -> None:
    """Without this the job sits in "running" forever: no error to retry, no
    error to report, and a worker slot held until the process is restarted."""

    async def scenario() -> None:
        runner = CommandRunner(tmp_path, default_timeout_seconds=0.5)
        started = time.monotonic()

        with pytest.raises(RuntimeError) as caught:
            await runner.run(
                "python",
                ["-c", "import time; time.sleep(60)"],
                "Hung command",
            )

        assert "timed out" in str(caught.value)
        assert "SUBPROCESS_TIMEOUT_SECONDS" in str(caught.value)
        # Terminated rather than merely abandoned.
        assert time.monotonic() - started < 30

    asyncio.run(scenario())


def test_a_command_finishing_inside_the_timeout_is_unaffected(tmp_path) -> None:
    async def scenario() -> None:
        runner = CommandRunner(tmp_path, default_timeout_seconds=30)
        result = await runner.run("python", ["-c", "print('done')"], "Quick command")
        assert "done" in result.stdout

    asyncio.run(scenario())


def test_timeout_can_be_disabled_per_call(tmp_path) -> None:
    """An explicit None opts one command out; the default still applies to the
    rest, so disabling is never accidental."""

    async def scenario() -> None:
        runner = CommandRunner(tmp_path, default_timeout_seconds=0.5)
        result = await runner.run(
            "python", ["-c", "print('ok')"], "Unbounded command", timeout_seconds=None
        )
        assert "ok" in result.stdout

    asyncio.run(scenario())


def test_a_non_positive_configured_timeout_disables_the_watchdog(tmp_path) -> None:
    """A misconfigured 0 must mean "no watchdog", never "time out instantly"."""
    runner = CommandRunner(tmp_path, default_timeout_seconds=0)
    assert runner.default_timeout_seconds is None


# --- database connections --------------------------------------------------


def test_one_database_layer_serves_the_whole_application() -> None:
    """The jobs queue used to run on a second, raw-SQL connection layer with its
    own pool and its own ``CREATE TABLE`` statements. Two pools against one file
    and two sources of truth for one schema is what this pins shut: every table
    is a model, and ``app.database`` exposes a single way to reach the database.
    """
    import app.database as database_package
    from app.database.models import Base

    assert database_package.__all__ == ["OrmDatabase"]
    for table in ("jobs", "job_attempts", "job_events"):
        assert table in Base.metadata.tables, f"{table} is described outside the ORM metadata again"


def test_the_orm_engine_is_disposed_on_shutdown(tmp_path) -> None:
    """Shutdown has to return the connections, not leave them to the collector."""

    async def scenario() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        await database.initialize()
        async with database.session() as db:
            await db.execute(sa_text("SELECT 1"))
        await database.shutdown()
        # A disposed engine drops its pooled connections; checked out size is the
        # observable part of that.
        assert database.engine.pool.checkedin() == 0

    asyncio.run(scenario())


# --- rate-limit keying behind a proxy --------------------------------------


class _StubClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _StubRequest:
    def __init__(self, host: str, forwarded: str | None = None) -> None:
        self.client = _StubClient(host)
        self.headers = {"x-forwarded-for": forwarded} if forwarded else {}


def test_without_a_proxy_the_socket_address_is_used() -> None:
    request = _StubRequest("203.0.113.9", forwarded="1.2.3.4")
    # The header is present but untrusted, so it must be ignored entirely.
    assert _client_ip(request, trusted_proxy_count=0) == "203.0.113.9"


def test_behind_one_proxy_the_last_forwarded_entry_is_used() -> None:
    """Each proxy appends the peer it saw, so the rightmost entry is the one our
    own proxy wrote — the only one a client cannot forge."""
    request = _StubRequest("10.0.0.1", forwarded="198.51.100.7")
    assert _client_ip(request, trusted_proxy_count=1) == "198.51.100.7"


def test_a_client_cannot_forge_its_way_into_a_fresh_bucket() -> None:
    """A client sending its own X-Forwarded-For prepends entries. Counting from
    the right means the forged values are ignored, so an attacker cannot mint a
    new rate-limit key per request."""
    request = _StubRequest("10.0.0.1", forwarded="evil-1, evil-2, 198.51.100.7")
    assert _client_ip(request, trusted_proxy_count=1) == "198.51.100.7"


def test_two_proxies_read_one_hop_further_left() -> None:
    request = _StubRequest("10.0.0.1", forwarded="198.51.100.7, 10.0.0.2")
    assert _client_ip(request, trusted_proxy_count=2) == "198.51.100.7"


def test_a_missing_header_falls_back_to_the_socket_address() -> None:
    request = _StubRequest("10.0.0.1")
    assert _client_ip(request, trusted_proxy_count=1) == "10.0.0.1"


def test_a_shorter_chain_than_configured_uses_the_leftmost_entry() -> None:
    """Misconfiguration must not hand every request the same key."""
    request = _StubRequest("10.0.0.1", forwarded="198.51.100.7")
    assert _client_ip(request, trusted_proxy_count=5) == "198.51.100.7"
