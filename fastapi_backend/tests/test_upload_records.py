import asyncio
import json
from copy import deepcopy

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import AppError
from app.database.orm import OrmDatabase
from app.repositories.session_repository import SessionRepository
from app.repositories.upload_repository import UploadRepository
from app.schemas.uploads import CompleteUploadRequest
from tests.test_session_maintenance import _init, _settings
from app.services.container import create_container


def upload_payload():
    return {"id": "u1", "sessionId": "s1", "status": "initiated", "expiresAt": "2099-01-01T00:00:00Z",
            "files": [{"fileId": "f1", "kind": "video", "sizeBytes": 1, "parts": [{"partNumber": 1, "sizeBytes": 1}]}]}


def test_upload_fk_optimistic_writes_and_cross_connection_locking(tmp_path):
    async def run():
        database = OrmDatabase(tmp_path / "uploads.sqlite3")
        other = OrmDatabase(tmp_path / "uploads.sqlite3")
        sessions = SessionRepository(database)
        repo = UploadRepository(database)
        peer = UploadRepository(other)
        await database.initialize()
        await other.initialize()
        with pytest.raises(IntegrityError):
            await repo.write(upload_payload())
        await sessions.write({"id": "s1", "name": "Session"})
        await repo.write(upload_payload())
        stale = await peer.read("u1")
        fresh = await repo.read("u1")
        fresh["status"] = "uploading"
        await repo.write(fresh)
        with pytest.raises(AppError):
            await peer.write(stale)

        async def append(repository, number):
            async with repository.locked("u1"):
                payload = await repository.read("u1")
                await asyncio.sleep(0.01)
                payload["files"][0]["parts"].append({"partNumber": number, "sizeBytes": 1})
                await repository.write(payload)
        await asyncio.gather(append(repo, 2), append(peer, 3))
        assert len((await repo.read("u1"))["files"][0]["parts"]) == 3
        await sessions.delete("s1")
        with pytest.raises(FileNotFoundError):
            await repo.read("u1")
        await other.shutdown()
        await database.shutdown()
    asyncio.run(run())


def test_legacy_import_is_restart_safe_and_preserves_bad_files(tmp_path):
    async def run():
        database = OrmDatabase(tmp_path / "uploads.sqlite3")
        sessions = SessionRepository(database)
        await sessions.write({"id": "s1", "name": "Session"})
        legacy = tmp_path / "legacy"
        legacy.mkdir()
        path = legacy / "u1.json"
        original = upload_payload()
        path.write_text(json.dumps(original))
        (legacy / "bad.json").write_text("not json")
        orphan = {**original, "id": "orphan", "sessionId": "missing"}
        (legacy / "orphan.json").write_text(json.dumps(orphan))
        repo = UploadRepository(database, legacy)
        assert await repo.migrate_legacy() == 1
        assert not path.exists()
        assert path.with_suffix(".json.migrated").exists()
        current = await repo.read("u1")
        current["status"] = "committed"
        await repo.write(current)
        path.write_text(json.dumps(original))
        assert await repo.migrate_legacy() == 0
        assert (await repo.read("u1"))["status"] == "committed"
        assert (legacy / "bad.json").exists()
        assert (legacy / "orphan.json").exists()
        await database.shutdown()
    asyncio.run(run())


def test_assembly_start_rolls_back_and_does_not_spawn_on_session_failure(tmp_path, monkeypatch):
    async def run():
        container = create_container(_settings(tmp_path))
        await _init(container)
        await container.sessions.write({"id": "s1", "name": "Session", "status": "waiting_for_upload"})
        repo = container.async_uploads.repository
        await repo.write(upload_payload())

        async def fail(*args, **kwargs):
            raise RuntimeError("injected failure")
        monkeypatch.setattr(container.sessions, "update", fail)
        with pytest.raises(RuntimeError, match="injected"):
            await container.async_uploads.complete("u1", CompleteUploadRequest())
        assert (await repo.read("u1"))["status"] == "initiated"
        assert container.async_uploads._background_tasks.active_count == 0
        await container.shutdown()
    asyncio.run(run())


def test_final_commit_rolls_back_upload_video_job_and_session(tmp_path, monkeypatch):
    async def run():
        container = create_container(_settings(tmp_path))
        await _init(container)
        await container.sessions.write({"id": "s1", "name": "Session", "status": "assembling"})
        job = await container.jobs.create_waiting_job("s1", "process_session")
        upload = {**upload_payload(), "status": "assembling", "jobId": job["id"]}
        await container.async_uploads.repository.write(upload)
        assembled = deepcopy(upload)
        assembled["files"].append({"fileId": "f2", "kind": "caseStudy"})

        async def fail(*args, **kwargs):
            raise RuntimeError("injected failure")
        monkeypatch.setattr(container.sessions, "update", fail)
        with pytest.raises(RuntimeError, match="injected"):
            await container.async_uploads._commit_assembled(assembled, True, {}, {}, None, False)
        assert (await container.async_uploads.repository.read("u1"))["status"] == "assembling"
        assert (await container.jobs.repository.read(job["id"]))["status"] == "waiting_for_upload"
        assert (await container.sessions.read("s1"))["status"] == "assembling"
        assert await container.videos.get_for_session("s1") is None
        await container.shutdown()
    asyncio.run(run())
