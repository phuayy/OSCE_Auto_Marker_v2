from __future__ import annotations

import asyncio
import logging

from app.database.orm import OrmDatabase
from app.repositories.session_repository import LOADED_VERSION_KEY, SessionRepository


def _make_repo(tmp_path) -> SessionRepository:
    return SessionRepository(OrmDatabase(tmp_path / "sessions.sqlite3"))


def test_read_stamps_optimistic_version_token(tmp_path) -> None:
    repo = _make_repo(tmp_path)

    async def scenario() -> None:
        await repo.write({"id": "s1", "name": "Case", "status": "uploaded", "createdAt": "2026-01-01T00:00:00Z"})
        loaded = await repo.read("s1")
        assert loaded[LOADED_VERSION_KEY]
        # The token must never leak into the persisted payload column.
        async with repo.database.session() as db_session:
            from app.database.models import SessionRecord

            record = await db_session.get(SessionRecord, "s1")
            assert LOADED_VERSION_KEY not in (record.payload or {})

    asyncio.run(scenario())


def test_concurrent_modification_is_detected_and_logged(tmp_path, caplog) -> None:
    repo = _make_repo(tmp_path)

    async def scenario() -> None:
        await repo.write({"id": "s1", "name": "Case", "status": "uploaded", "createdAt": "2026-01-01T00:00:00Z"})

        # Two readers load the same version (simulating a worker and a request
        # handler both reading before either writes).
        worker_view = await repo.read("s1")
        stale_view = await repo.read("s1")

        # Worker commits first — advances updated_at.
        worker_view["status"] = "completed"
        await repo.write(worker_view)

        # The stale writer should be flagged as overwriting newer state.
        with caplog.at_level(logging.WARNING):
            stale_view["status"] = "processing"
            await repo.write(stale_view)

        assert any("Concurrent session modification detected" in rec.message for rec in caplog.records)

    asyncio.run(scenario())


def test_no_false_positive_on_read_once_write_many(tmp_path, caplog) -> None:
    """The pipeline reads a session ONCE then writes it many times (marking each
    step). Re-stamping the in-memory version on write must keep that from being
    flagged as a concurrent modification."""
    repo = _make_repo(tmp_path)

    async def scenario() -> None:
        await repo.write({"id": "s1", "name": "Case", "status": "uploaded", "createdAt": "2026-01-01T00:00:00Z"})
        view = await repo.read("s1")  # single read
        with caplog.at_level(logging.WARNING):
            for status in ("processing", "transcribing", "scoring", "completed"):
                view["status"] = status
                await repo.write(view)  # many writes of the SAME dict
        assert not any("Concurrent session modification detected" in rec.message for rec in caplog.records)

    asyncio.run(scenario())


def test_no_false_positive_on_sequential_read_modify_write(tmp_path, caplog) -> None:
    repo = _make_repo(tmp_path)

    async def scenario() -> None:
        await repo.write({"id": "s1", "name": "Case", "status": "uploaded", "createdAt": "2026-01-01T00:00:00Z"})
        with caplog.at_level(logging.WARNING):
            for status in ("processing", "completed"):
                view = await repo.read("s1")
                view["status"] = status
                await repo.write(view)
        assert not any("Concurrent session modification detected" in rec.message for rec in caplog.records)

    asyncio.run(scenario())
