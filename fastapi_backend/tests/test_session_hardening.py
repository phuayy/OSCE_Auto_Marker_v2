import asyncio
import json
from pathlib import Path

import pytest
from app.core.artifacts import artifact_metadata, read_artifact_payload
from app.core.config import Settings
from app.core.exceptions import StaleSessionError
from app.database.orm import OrmDatabase
from app.repositories.session_repository import SessionRepository
from app.services.event_service import EventService
from app.services.session_service import SessionService


class ProjectionRepository(SessionRepository):
    async def read_all(self):
        raise AssertionError("A request must not scan full session payloads")


def build_service(path: Path):
    database = OrmDatabase(path / "sessions.sqlite3")
    settings = Settings(root_dir=path, backend_root=path, ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", scorer_python_bin="python")
    return SessionService(settings, ProjectionRepository(database)), database


def test_parallel_named_creates_across_connections_are_unique(tmp_path):
    async def run():
        first, first_db = build_service(tmp_path)
        second, second_db = build_service(tmp_path)
        await first_db.initialize()
        await second_db.initialize()
        try:
            rows = [{"id": str(index), "name": "A" * 80, "outputs": {}} for index in range(12)]
            await asyncio.gather(*[
                (first if index % 2 else second).create_named(row)
                for index, row in enumerate(rows)
            ])
            names = {row["name"] for row in rows}
            assert len(names) == 12
            assert all(len(name) <= 80 for name in names)
            assert "A" * 78 + " 2" in names
            with pytest.raises(FileExistsError):
                await first.rename_session("1", rows[0]["name"])
        finally:
            await first_db.engine.dispose()
            await second_db.engine.dispose()
    asyncio.run(run())


def test_name_backfill_and_rename_preserve_outputs_without_full_scan(tmp_path):
    async def run():
        service, db = build_service(tmp_path)
        try:
            output = {"scores": {"payload": {"large": "x" * 100_000}}}
            await service.write({"id": "s1", "outputs": output, "files": {"video": {"originalName": "Station.mp4"}}})
            index = await service.list_sessions()
            assert index[0]["name"] == "Station"
            stale = await service.read("s1")
            renamed = await service.rename_session("s1", "Renamed")
            assert renamed["outputs"] == output
            stale["status"] = "processing"
            with pytest.raises(StaleSessionError):
                await service.write(stale)
            assert (await service.read("s1"))["name"] == "Renamed"
        finally:
            await db.engine.dispose()
    asyncio.run(run())


def test_child_projection_only_returns_matching_children_and_output_paths(tmp_path):
    async def run():
        service, db = build_service(tmp_path)
        try:
            for sid, parent in [("child", "parent"), ("other", "elsewhere")]:
                await service.write({
                    "id": sid, "name": sid, "parentSessionId": parent,
                    "clipSource": {"clipId": "clip1"},
                    "outputs": {"scores": {"absolutePath": "/scores/child.json", "payload": {"large": "x" * 100_000}}},
                })
            entries = await service.repository.child_summaries("parent")
            assert [entry.session["id"] for entry in entries] == ["child"]
            assert entries[0].session["outputs"]["scores"] == {"absolutePath": "/scores/child.json"}
            assert "large" not in json.dumps(entries[0].session)
        finally:
            await db.engine.dispose()
    asyncio.run(run())


def test_public_session_is_small_and_artifacts_remain_readable(tmp_path):
    service, _db = build_service(tmp_path)
    payload = {"criteria": [{"evidence": "x" * 1_000_000}]}
    path = tmp_path / "score.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    metadata = artifact_metadata(path, "/media/scores")
    session = {"id": "s1", "outputs": {"scores": {**metadata, "payload": payload}}}
    public = service.public_session(session)
    assert len(json.dumps(public)) < 2500
    assert "payload" not in public["outputs"]["scores"]
    assert asyncio.run(service.read_output_payload(session, "scores")) == payload
    assert asyncio.run(read_artifact_payload({"payload": payload})) == payload


def test_default_session_sse_retains_no_history_and_opt_in_still_delivers():
    async def run():
        disabled = EventService(Settings(session_sse_enabled=False))
        await disabled.publish("s1", "log", {"message": "unused"})
        assert disabled._states == {}
        enabled = EventService(Settings(session_sse_enabled=True))
        state = enabled.get_state("s1")
        queue = asyncio.Queue(maxsize=2)
        state.queues[1] = queue
        state.clients.add(1)
        await enabled.publish("s1", "status", {"code": "processing"})
        event, payload = queue.get_nowait()
        assert event == "status"
        assert payload["code"] == "processing"
        assert payload["id"] == 1
        assert payload["at"]
    asyncio.run(run())
