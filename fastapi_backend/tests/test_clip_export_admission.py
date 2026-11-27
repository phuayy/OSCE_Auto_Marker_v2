"""Who may start a clip export, and what a failed start leaves behind.

Export, recrop and auto-crop all rewrite the session's clip list, so each must
refuse while another owns it: a job-driven status (queued included, not only
processing) or a live clip export. The check and the claim are one write, and a
start that dies after the claim releases it, so a stranded ``queued`` record can
never lock exports out.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.core.exceptions import AppError
from app.domain.sessions import clip_list_busy, export_identity
from app.services.container import create_container
from tests.test_clip_export_job import _build
from tests.test_session_maintenance import _init, _settings


def _export(service, boundaries=(200.0,)):
    return asyncio.run(service.request_clip_export("sess-1", list(boundaries), [], None))


def _status(error: pytest.ExceptionInfo[AppError]) -> int:
    return error.value.status_code


# --- the shared predicate -------------------------------------------------


def test_predicate_counts_queued_sessions_and_live_exports() -> None:
    assert clip_list_busy({"status": "queued"})
    assert clip_list_busy({"status": "processing"})
    assert not clip_list_busy({"status": "cropped", "clipExport": {"status": "completed"}})
    live = {"status": "cropped", "clipExport": {"status": "running", "planId": "p", "jobId": "j"}}
    assert clip_list_busy(live)


def test_predicate_ignores_only_the_record_judged_dead() -> None:
    record = {"status": "queued", "planId": "p", "jobId": "j", "requestedAt": "t1"}
    session = {"status": "cropped", "clipExport": record}
    assert not clip_list_busy(session, settled_export=export_identity(record))
    # A concurrent request replaced it since: the new record is live.
    session["clipExport"] = {**record, "planId": "q", "requestedAt": "t2"}
    assert clip_list_busy(session, settled_export=export_identity(record))


# --- M8: session status ---------------------------------------------------


@pytest.mark.parametrize("status", ["queued", "processing"])
def test_export_is_refused_while_the_session_is_job_driven(tmp_path: Path, status: str) -> None:
    service, sessions, _events, _media, jobs = _build(tmp_path)
    sessions.current["status"] = status

    with pytest.raises(AppError) as error:
        _export(service)

    assert _status(error) == 409
    assert jobs.enqueued == []
    assert "clipExport" not in sessions.current


def test_recrop_is_refused_while_auto_crop_is_queued(tmp_path: Path) -> None:
    service, sessions, _events, _media, jobs = _build(tmp_path)
    _export(service)
    clip_id = str(sessions.current["outputs"]["videoClips"][0]["id"])
    jobs.repository.rows["job-1"]["status"] = "succeeded"
    sessions.current["status"] = "queued"
    before = len(jobs.enqueued)

    with pytest.raises(AppError) as error:
        asyncio.run(service.request_clip_recrop("sess-1", clip_id, 10.0, 80.0))

    assert _status(error) == 409
    assert len(jobs.enqueued) == before


def test_a_normal_export_on_a_cropped_session_is_accepted(tmp_path: Path) -> None:
    service, sessions, _events, _media, jobs = _build(tmp_path)
    sessions.current["status"] = "cropped"

    result = _export(service)

    assert result["clipExport"]["status"] == "queued"
    assert len(jobs.enqueued) == 1


# --- M11: the live check is judged by the job row --------------------------


def test_concurrent_exports_admit_exactly_one(tmp_path: Path) -> None:
    service, _sessions, _events, _media, jobs = _build(tmp_path)

    async def both():
        return await asyncio.gather(
            service.request_clip_export("sess-1", [200.0], [], None),
            service.request_clip_export("sess-1", [300.0], [], None),
            return_exceptions=True,
        )

    results = asyncio.run(both())

    refused = [r for r in results if isinstance(r, AppError)]
    assert len(refused) == 1 and refused[0].status_code == 409
    assert len(jobs.enqueued) == 1


def test_a_failed_enqueue_fails_the_record_and_the_next_export_succeeds(tmp_path: Path) -> None:
    service, sessions, _events, _media, jobs = _build(tmp_path)
    jobs.enqueue_error = RuntimeError("queue down")

    with pytest.raises(AppError) as error:
        _export(service)

    assert _status(error) == 503
    export = sessions.current["clipExport"]
    assert export["status"] == "failed" and export["error"] and export["endedAt"]

    jobs.enqueue_error = None
    result = _export(service, (300.0,))
    assert result["clipExport"]["status"] == "queued"
    assert result["clipExport"]["jobId"] == "job-1"


def test_a_failed_recrop_enqueue_releases_the_same_way(tmp_path: Path) -> None:
    service, sessions, _events, _media, jobs = _build(tmp_path)
    _export(service)
    jobs.repository.rows["job-1"]["status"] = "succeeded"
    clip_id = str(sessions.current["outputs"]["videoClips"][0]["id"])
    asyncio.run(service.export_clips_by_id("sess-1"))
    jobs.enqueue_error = RuntimeError("queue down")

    with pytest.raises(AppError):
        asyncio.run(service.request_clip_recrop("sess-1", clip_id, 10.0, 80.0))

    assert sessions.current["clipExport"]["status"] == "failed"
    jobs.enqueue_error = None
    asyncio.run(service.request_clip_recrop("sess-1", clip_id, 10.0, 80.0))
    assert sessions.current["clipExport"]["status"] == "queued"


def test_a_queued_record_with_no_job_id_and_no_active_job_is_not_live(tmp_path: Path) -> None:
    service, sessions, _events, _media, _jobs = _build(tmp_path)
    sessions.current["clipExport"] = {"planId": "old", "status": "queued", "jobId": None, "requestedAt": "t0"}

    assert _export(service)["clipExport"]["status"] == "queued"


def test_a_queued_record_with_no_job_id_but_an_active_job_is_live(tmp_path: Path) -> None:
    service, sessions, _events, _media, jobs = _build(tmp_path)
    asyncio.run(jobs.enqueue("sess-1", "export_clips", {}))
    sessions.current["clipExport"] = {"planId": "old", "status": "queued", "jobId": None, "requestedAt": "t0"}

    with pytest.raises(AppError) as error:
        _export(service)
    assert _status(error) == 409


def test_a_record_whose_job_was_cancelled_is_not_live(tmp_path: Path) -> None:
    service, sessions, _events, _media, jobs = _build(tmp_path)
    _export(service)
    jobs.repository.rows["job-1"]["status"] = "cancelled"
    assert sessions.current["clipExport"]["status"] == "queued"

    assert _export(service, (300.0,))["clipExport"]["jobId"] == "job-2"


def test_an_unreadable_job_row_never_admits_a_second_export(tmp_path: Path) -> None:
    service, sessions, _events, _media, jobs = _build(tmp_path)
    _export(service)

    async def broken_read(_job_id: str):
        raise ConnectionError("pool timeout")

    jobs.repository.read = broken_read  # type: ignore[method-assign]

    with pytest.raises(ConnectionError):
        _export(service, (300.0,))
    assert len(jobs.enqueued) == 1
    assert sessions.current["clipExport"]["jobId"] == "job-1"


def test_a_record_whose_job_is_gone_is_not_live(tmp_path: Path) -> None:
    service, sessions, _events, _media, jobs = _build(tmp_path)
    _export(service)
    del jobs.repository.rows["job-1"]

    assert _export(service, (300.0,))["clipExport"]["status"] == "queued"


# --- M8: auto-crop admission against a live export -------------------------


def _long_session(tmp_path: Path, **extra: Any) -> dict[str, Any]:
    video = tmp_path / "long.mp4"
    video.write_bytes(b"x")
    return {
        "id": "long-1",
        "name": "Long",
        "status": "cropped",
        "workflow": "long",
        "files": {"video": {"fileName": "long.mp4", "absolutePath": str(video)}},
        "outputs": {"videoClips": [{"id": "c1", "isDraft": True}]},
        **extra,
    }


async def _container(tmp_path: Path, **session_extra: Any):
    container = create_container(_settings(tmp_path))
    await _init(container)

    async def _noop(_job) -> None:
        return None

    container.jobs._dispatch = _noop  # type: ignore[method-assign]
    await container.sessions.write(_long_session(tmp_path, **session_extra))
    return container


def _live_export(job_id: str | None) -> dict[str, Any]:
    return {
        "planId": "p1", "status": "running", "jobId": job_id, "requestedAt": "t0",
        "scope": "plan", "clipIds": None, "total": 2, "completed": 0,
    }


@pytest.mark.parametrize("action", ["start_auto_crop", "rerun_session"])
def test_auto_crop_is_refused_while_an_export_is_live(tmp_path: Path, action: str) -> None:
    async def run() -> None:
        container = await _container(tmp_path)
        job = await container.jobs.enqueue("long-1", "export_clips", {})
        await container.sessions.update("long-1", lambda s: s.update(clipExport=_live_export(job["id"])))

        with pytest.raises(AppError) as error:
            await getattr(container.session_maintenance, action)("long-1")

        assert error.value.status_code == 409
        session = await container.sessions.read("long-1")
        assert session["status"] == "cropped"
        assert session["clipExport"]["status"] == "running"
        assert [j["taskType"] for j in await container.jobs.list_jobs("long-1")] == ["export_clips"]
        await container.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("action", ["start_auto_crop", "rerun_session"])
def test_auto_crop_replaces_an_export_record_whose_job_is_dead(tmp_path: Path, action: str) -> None:
    async def run() -> None:
        container = await _container(tmp_path)
        await container.sessions.update("long-1", lambda s: s.update(clipExport=_live_export(None)))

        result = await getattr(container.session_maintenance, action)("long-1")

        assert result["job"]["taskType"] == "auto_crop"
        await container.shutdown()

    asyncio.run(run())
