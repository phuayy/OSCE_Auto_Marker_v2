"""Audit 2026-09-29 findings 4, 5 and 6: partial failures in session teardown,
job admission, and object-storage deletion must be reported or absorbed
correctly, not papered over.

* A database step of a delete that fails must fail the delete — the session
  must not be reported deleted, and its files must not be removed from under a
  row that still exists.
* A queue insertion that fails must not leave the session ``queued`` with no
  job (un-startable until a restart reconciles it).
* Two concurrent starts of one session must admit exactly one.
* A top-level session's committed video object is deleted through object
  storage, not just unlinked locally — best-effort on delete (the row is
  already gone), required (awaited before ``purgedAt`` is set) on purge — and
  never for a clip child, whose video is its parent's exported clip.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.core.exceptions import AppError
from app.services.container import create_container
from tests.test_session_maintenance import _init, _score_payload, _settings


async def _container(tmp_path):
    container = create_container(_settings(tmp_path))
    await _init(container)

    async def _noop_dispatch(_job) -> None:
        return None

    container.jobs._dispatch = _noop_dispatch  # type: ignore[method-assign]
    return container


async def _seed(container, tmp_path, session_id: str = "s1", status: str = "completed") -> dict:
    video = tmp_path / f"{session_id}.mp4"
    score = tmp_path / f"{session_id}-scores.json"
    video.write_text("x", encoding="utf-8")
    score.write_text(json.dumps(_score_payload()), encoding="utf-8")
    session = {
        "id": session_id,
        "name": f"Session {session_id}",
        "status": status,
        "files": {"video": {"fileName": video.name, "absolutePath": str(video)}},
        "outputs": {"scores": {"absolutePath": str(score), "payload": _score_payload()}},
    }
    await container.sessions.write(session)
    return {"video": video, "score": score}


# --- finding 5: delete -------------------------------------------------------


@pytest.mark.parametrize("failing_step", ["session_row", "jobs"])
def test_a_failed_database_step_fails_the_delete_and_keeps_the_files(tmp_path, monkeypatch, failing_step) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        files = await _seed(container, tmp_path)

        async def _boom(*_args, **_kwargs):
            raise RuntimeError("simulated database failure")

        if failing_step == "session_row":
            monkeypatch.setattr(container.sessions.repository, "delete", _boom)
        else:
            monkeypatch.setattr(container.jobs, "purge_session", _boom)

        with pytest.raises(Exception):
            await container.session_maintenance.delete_session("s1")

        # Still there, and still whole: a retry can finish the job.
        assert (await container.sessions.read("s1"))["id"] == "s1"
        assert files["video"].exists()
        assert files["score"].exists()
        await container.shutdown()

    asyncio.run(_run())


def test_a_delete_that_failed_can_be_retried_to_completion(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        files = await _seed(container, tmp_path)
        real_delete = container.sessions.repository.delete
        calls = {"n": 0}

        async def _flaky(session_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated database failure")
            return await real_delete(session_id)

        monkeypatch.setattr(container.sessions.repository, "delete", _flaky)
        with pytest.raises(Exception):
            await container.session_maintenance.delete_session("s1")
        result = await container.session_maintenance.delete_session("s1")
        assert result == {"deletedSessionIds": ["s1"]}
        with pytest.raises(FileNotFoundError):
            await container.sessions.read("s1")
        assert not files["video"].exists()
        await container.shutdown()

    asyncio.run(_run())


# --- finding 6: admission ----------------------------------------------------


@pytest.mark.parametrize("entry", ["start_processing", "rerun_session"])
def test_a_failed_enqueue_does_not_strand_the_session_as_queued(tmp_path, monkeypatch, entry) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        await _seed(container, tmp_path, status="failed" if entry == "rerun_session" else "uploaded")

        async def _boom(*_args, **_kwargs):
            raise RuntimeError("simulated enqueue failure")

        monkeypatch.setattr(container.jobs, "enqueue", _boom)
        with pytest.raises(Exception):
            await getattr(container.session_maintenance, entry)("s1")

        session = await container.sessions.read("s1")
        assert session["status"] not in {"queued", "processing"}
        assert session.get("error")
        assert await container.jobs.list_jobs("s1") == []

        # ...and the session can be started again without a restart.
        monkeypatch.undo()
        container.jobs._dispatch = _noop  # type: ignore[method-assign]
        result = await container.session_maintenance.rerun_session("s1")
        assert result["job"] is not None
        await container.shutdown()

    asyncio.run(_run())


async def _noop(_job) -> None:
    return None


@pytest.mark.parametrize("entry", ["start_processing", "rerun_session"])
def test_concurrent_starts_admit_exactly_one(tmp_path, entry) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        await _seed(container, tmp_path, status="uploaded")
        start = getattr(container.session_maintenance, entry)

        results = await asyncio.gather(start("s1"), start("s1"), return_exceptions=True)
        admitted = [r for r in results if isinstance(r, dict)]
        refused = [r for r in results if isinstance(r, AppError)]
        assert len(admitted) == 1, results
        assert len(refused) == 1 and refused[0].status_code == 409, results
        assert len(await container.jobs.list_jobs("s1")) == 1
        await container.shutdown()

    asyncio.run(_run())


# --- finding 4: storage-aware deletion of the committed video ---------------


class _FakeStorage:
    """A minimal double for the ``ObjectStorage`` protocol's
    ``delete_committed_object`` — the only method this module calls on it."""

    def __init__(self, *, raise_on_delete: bool = False) -> None:
        self.calls: list[dict] = []
        self.raise_on_delete = raise_on_delete

    async def delete_committed_object(self, storage_ref: dict) -> bool:
        self.calls.append(storage_ref)
        if self.raise_on_delete:
            raise RuntimeError("simulated storage failure")
        return True


async def _seed_with_storage_ref(container, tmp_path, session_id: str, storage_ref: dict, **extra) -> Path:
    video = tmp_path / f"{session_id}.mp4"
    video.write_text("x", encoding="utf-8")
    session = {
        "id": session_id,
        "name": f"Session {session_id}",
        "status": "completed",
        "files": {"video": {"fileName": video.name, "absolutePath": str(video), "storageRef": storage_ref}},
        "outputs": {},
        **extra,
    }
    await container.sessions.write(session)
    return video


def test_purge_calls_delete_committed_object_before_marking_purged(tmp_path) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        session_id = "s-storage-1"
        storage_ref = {"provider": "gcs", "bucket": "b", "key": "k1"}
        await _seed_with_storage_ref(container, tmp_path, session_id, storage_ref)
        fake = _FakeStorage()
        container.session_maintenance.storage = fake

        purged = await container.session_maintenance.purge_expired_video(session_id)

        assert purged is True
        assert fake.calls == [storage_ref]
        reloaded = await container.sessions.read(session_id)
        assert reloaded["files"]["video"]["purgedAt"]
        await container.shutdown()

    asyncio.run(_run())


def test_purge_raising_delete_leaves_purged_at_unset(tmp_path) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        session_id = "s-storage-2"
        storage_ref = {"provider": "gcs", "bucket": "b", "key": "k2"}
        video = await _seed_with_storage_ref(container, tmp_path, session_id, storage_ref)
        fake = _FakeStorage(raise_on_delete=True)
        container.session_maintenance.storage = fake

        with pytest.raises(Exception):
            await container.session_maintenance.purge_expired_video(session_id)

        reloaded = await container.sessions.read(session_id)
        assert not (reloaded["files"]["video"] or {}).get("purgedAt")
        # The object delete is awaited before the local unlink; a raise there
        # must leave the local file alone too, not just the DB record.
        assert video.exists()
        await container.shutdown()

    asyncio.run(_run())


def test_purge_never_deletes_a_child_sessions_shared_object(tmp_path) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        clip = tmp_path / "clip.mp4"
        clip.write_text("x", encoding="utf-8")
        child_id = "child-storage"
        await container.sessions.write(
            {
                "id": child_id,
                "name": "Student A",
                "status": "completed",
                "parentSessionId": "parent-storage",
                "files": {
                    "video": {
                        "fileName": "clip.mp4",
                        "absolutePath": str(clip),
                        "storageRef": {"provider": "gcs", "bucket": "b", "key": "k-child"},
                    },
                },
                "outputs": {},
            }
        )
        fake = _FakeStorage()
        container.session_maintenance.storage = fake

        purged = await container.session_maintenance.purge_expired_video(child_id)

        assert purged is False
        assert fake.calls == []
        assert clip.exists()
        await container.shutdown()

    asyncio.run(_run())


def test_delete_of_a_top_level_session_calls_delete_committed_object_best_effort(tmp_path) -> None:
    """A failed object delete must not fail the session delete: the row is
    already gone by the time it runs, so there is nothing left to mark."""

    async def _run() -> None:
        container = await _container(tmp_path)
        session_id = "s-storage-3"
        storage_ref = {"provider": "gcs", "bucket": "b", "key": "k3"}
        await _seed_with_storage_ref(container, tmp_path, session_id, storage_ref)
        fake = _FakeStorage(raise_on_delete=True)
        container.session_maintenance.storage = fake

        result = await container.session_maintenance.delete_session(session_id)

        assert result == {"deletedSessionIds": [session_id]}
        assert fake.calls == [storage_ref]
        with pytest.raises(FileNotFoundError):
            await container.sessions.read(session_id)
        await container.shutdown()

    asyncio.run(_run())


def test_a_refused_rerun_never_deletes_the_running_sessions_artifacts(tmp_path) -> None:
    """Admission happens before the destructive cleanup: a rerun that loses the
    race must not have wiped the winner's outputs on its way to the 409."""

    async def _run() -> None:
        container = await _container(tmp_path)
        files = await _seed(container, tmp_path, status="failed")
        first = await container.session_maintenance.rerun_session("s1")
        assert first["job"] is not None
        # The winner's run has since written a fresh score file.
        files["score"].write_text(json.dumps(_score_payload()), encoding="utf-8")

        def _put_score(current):
            current.setdefault("outputs", {})["scores"] = {"absolutePath": str(files["score"])}
            return None

        await container.sessions.update("s1", _put_score)
        with pytest.raises(AppError) as refused:
            await container.session_maintenance.rerun_session("s1")
        assert refused.value.status_code == 409
        assert files["score"].exists()
        await container.shutdown()

    asyncio.run(_run())


def test_a_rerun_whose_cleanup_fails_after_admission_is_not_stranded_as_queued(tmp_path, monkeypatch) -> None:
    """Admission flips the session to queued before the destructive cleanup;
    if that cleanup fails there is no job coming, so the admission must be
    released rather than left looking in flight."""

    async def _run() -> None:
        container = await _container(tmp_path)
        await _seed(container, tmp_path, status="failed")

        async def _boom(*_args, **_kwargs):
            raise RuntimeError("simulated database failure")

        monkeypatch.setattr(container.assessments, "delete_session_results", _boom)
        with pytest.raises(AppError):
            await container.session_maintenance.rerun_session("s1")

        session = await container.sessions.read("s1")
        assert session["status"] == "failed"
        assert session.get("error")
        assert await container.jobs.list_jobs("s1") == []
        await container.shutdown()

    asyncio.run(_run())
