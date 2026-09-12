"""The session write contract.

A session is one JSON document that several writers touch during a run: the
pipeline (in this process or a Hatchet worker), the job queue, the export job,
and the user renaming things in the browser. The repository used to *detect*
a write based on a stale read and then overwrite anyway; these tests pin the
contract that replaced it — a stale write is refused, a document nobody read
cannot replace an existing row, and ``SessionService.update`` is how a change
is applied on top of whatever landed first.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import Settings
from app.core.exceptions import SessionWriteContractError, StaleSessionError
from app.database.orm import OrmDatabase
from app.repositories.session_repository import LOADED_VERSION_KEY, SessionRepository
from app.services.session_service import SessionService


def _make_repo(tmp_path) -> SessionRepository:
    return SessionRepository(OrmDatabase(tmp_path / "sessions.sqlite3"))


def _make_service(tmp_path) -> SessionService:
    settings = Settings(root_dir=tmp_path, backend_root=tmp_path)
    return SessionService(settings, _make_repo(tmp_path))


def _seed() -> dict:
    return {"id": "s1", "name": "Case", "status": "uploaded", "createdAt": "2026-01-01T00:00:00Z"}


def test_read_stamps_optimistic_version_token(tmp_path) -> None:
    repo = _make_repo(tmp_path)

    async def scenario() -> None:
        await repo.write(_seed())
        loaded = await repo.read("s1")
        assert loaded[LOADED_VERSION_KEY]
        # The token must never leak into the persisted payload column.
        async with repo.database.session() as db_session:
            from app.database.models import SessionRecord

            record = await db_session.get(SessionRecord, "s1")
            assert LOADED_VERSION_KEY not in (record.payload or {})

    asyncio.run(scenario())


def test_a_stale_write_is_refused_not_overwritten(tmp_path) -> None:
    """Two readers load the same version; the second to write loses — loudly."""
    repo = _make_repo(tmp_path)

    async def scenario() -> None:
        await repo.write(_seed())
        worker_view = await repo.read("s1")
        stale_view = await repo.read("s1")

        worker_view["status"] = "completed"
        await repo.write(worker_view)

        stale_view["status"] = "processing"
        with pytest.raises(StaleSessionError) as raised:
            await repo.write(stale_view)
        assert raised.value.status_code == 409

        # The first writer's state is what the store holds.
        assert (await repo.read("s1"))["status"] == "completed"

    asyncio.run(scenario())


def test_a_document_nobody_read_cannot_replace_an_existing_row(tmp_path) -> None:
    """The F3 guard: a projection (or any hand-built dict) aimed at a stored row
    is refused, because storing it would replace the payload with its keys."""
    repo = _make_repo(tmp_path)

    async def scenario() -> None:
        await repo.write({**_seed(), "files": {"video": {"originalName": "a.mp4"}}})
        projection = {"id": "s1", "name": "Case", "status": "failed", "hasVideoClips": False}
        with pytest.raises(SessionWriteContractError):
            await repo.write(projection)
        stored = await repo.read("s1")
        assert stored["files"] == {"video": {"originalName": "a.mp4"}}
        assert stored["status"] == "uploaded"

    asyncio.run(scenario())


def test_creating_a_new_row_needs_no_token(tmp_path) -> None:
    repo = _make_repo(tmp_path)

    async def scenario() -> None:
        await repo.write(_seed())  # no stamp, no row yet: a create
        assert (await repo.read("s1"))["name"] == "Case"

    asyncio.run(scenario())


def test_read_once_write_many_from_one_holder_is_fine(tmp_path) -> None:
    """One caller marking several steps on one object, with nobody else writing,
    must keep passing: each write re-stamps the dict to the version it stored."""
    repo = _make_repo(tmp_path)

    async def scenario() -> None:
        await repo.write(_seed())
        view = await repo.read("s1")
        for status in ("processing", "transcribing", "scoring", "completed"):
            view["status"] = status
            await repo.write(view)
        assert (await repo.read("s1"))["status"] == "completed"

    asyncio.run(scenario())


def test_update_replays_the_mutation_on_top_of_a_concurrent_write(tmp_path) -> None:
    """The F2 fix: a long-lived holder's change is expressed as a mutator, so
    when a rename lands mid-run the mutator is re-applied to the renamed
    document and both changes survive."""
    service = _make_service(tmp_path)

    async def scenario() -> None:
        await service.write({**_seed(), "pipeline": {"steps": {}}})
        pipeline_view = await service.read("s1")  # the "worker" loaded this an hour ago

        # A user renames the session in the browser.
        await service.update("s1", lambda s: s.__setitem__("name", "Alice's station"))

        # The worker marks a step, expressing it as a patch — not by writing its copy.
        def mark_step(session: dict) -> None:
            session.setdefault("pipeline", {}).setdefault("steps", {})["transcription"] = {"status": "completed"}

        fresh = await service.update(str(pipeline_view["id"]), mark_step)

        assert fresh["name"] == "Alice's station"
        assert fresh["pipeline"]["steps"]["transcription"]["status"] == "completed"
        # ...and the worker's stale copy, written whole, is still refused.
        pipeline_view["status"] = "completed"
        with pytest.raises(StaleSessionError):
            await service.write(pipeline_view)

    asyncio.run(scenario())


def test_update_returning_false_writes_nothing(tmp_path) -> None:
    service = _make_service(tmp_path)

    async def scenario() -> None:
        await service.write(_seed())
        before = (await service.read("s1"))[LOADED_VERSION_KEY]
        await service.update("s1", lambda _s: False)
        after = (await service.read("s1"))[LOADED_VERSION_KEY]
        assert before == after

    asyncio.run(scenario())


def test_update_retries_when_the_row_moves_under_it(tmp_path, monkeypatch) -> None:
    """Simulate a writer sneaking in between update's read and write."""
    service = _make_service(tmp_path)
    repo = service.repository
    original_write = repo.write
    sneaked = {"done": False}

    async def racing_write(session: dict) -> None:
        if not sneaked["done"]:
            sneaked["done"] = True
            other = await repo.read(str(session["id"]))
            other["name"] = "Renamed meanwhile"
            await original_write(other)
        await original_write(session)

    async def scenario() -> None:
        await service.write(_seed())
        monkeypatch.setattr(repo, "write", racing_write)
        fresh = await service.update("s1", lambda s: s.__setitem__("status", "queued"))
        assert fresh["status"] == "queued"
        assert fresh["name"] == "Renamed meanwhile"

    asyncio.run(scenario())
