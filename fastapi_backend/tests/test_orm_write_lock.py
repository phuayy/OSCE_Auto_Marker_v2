"""B1: OrmDatabase.ensure_write_locked — the SQLite upfront write lock a
read-then-write transaction() call takes to avoid SQLITE_BUSY_SNAPSHOT.

See the JobRepository class docstring (app/repositories/job_repository.py)
for the failure this exists to prevent, and
tests/test_job_repository.py::test_finish_takes_the_write_lock_before_reading
for proof it runs before the read in a real repository method.
"""

from __future__ import annotations

import asyncio

from app.database.orm import OrmDatabase


def test_ensure_write_locked_takes_the_lock_and_is_idempotent(tmp_path) -> None:
    async def scenario() -> None:
        database = OrmDatabase(tmp_path / "t.sqlite3")
        await database.initialize()
        async with database.transaction() as db:
            assert db.info.get("write_locked") is not True
            await database.ensure_write_locked(db)
            assert db.info.get("write_locked") is True
            # A second call on the same session must not try to BEGIN a
            # transaction that is already open — SQLite refuses that outright.
            await database.ensure_write_locked(db)

    asyncio.run(scenario())


def test_unit_of_work_takes_the_same_lock(tmp_path) -> None:
    """unit_of_work() opens the transaction ensure_write_locked defends —
    both must agree on the flag so a transaction() joining an existing
    unit_of_work() does not redundantly re-issue BEGIN IMMEDIATE."""

    async def scenario() -> None:
        database = OrmDatabase(tmp_path / "t.sqlite3")
        async with database.unit_of_work() as db:
            assert db.info.get("write_locked") is True
            # Idempotent even though the lock was already taken by unit_of_work.
            await database.ensure_write_locked(db)

    asyncio.run(scenario())


def test_ensure_write_locked_is_a_no_op_outside_sqlite(tmp_path, monkeypatch) -> None:
    """PostgreSQL's MVCC has no equivalent snapshot-upgrade failure for a
    plain read-then-write in one transaction — row-level locking on the later
    UPDATE is enough — so this must never try to run SQLite's BEGIN IMMEDIATE
    against it."""

    async def scenario() -> None:
        database = OrmDatabase(tmp_path / "t.sqlite3")
        # Initialize (runs the real SQLite migration) before pretending to be
        # a different dialect — ensure_write_locked is the only thing under
        # test here, not a second migration run against the wrong SQL dialect.
        await database.initialize()
        monkeypatch.setattr(database.engine.dialect, "name", "postgresql")
        async with database.transaction() as db:
            await database.ensure_write_locked(db)
            assert db.info.get("write_locked") is not True

    asyncio.run(scenario())
