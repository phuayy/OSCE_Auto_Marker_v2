"""Cross-service integrity of the session lifecycle.

Each test here reproduces a defect that the audit found by driving the real
services end to end — the clip export job racing a rename, a re-split adopting
the previous split's footage, startup recovery writing a projection back as a
session, a job that dies at the claim, parts uploaded in parallel. They stay as
the executable statement of the contracts that replaced those behaviours.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.database.orm import OrmDatabase
from app.database.orm import OrmDatabase
from app.repositories.job_repository import JobRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.upload_repository import UploadRepository
from app.services.event_service import EventService
from app.services.job_queue_service import JobQueueService
from app.services.session_service import SessionService
from app.storage.local import LocalObjectStorageService

from tests.test_clip_export_job import _build as build_clip_harness


# --- F1: a re-split never adopts the previous plan's MP4s --------------------


def test_a_resplit_cuts_every_clip_of_the_new_plan(tmp_path) -> None:
    service, sessions, _events, media, jobs = build_clip_harness(tmp_path)
    asyncio.run(service.request_clip_export("sess-1", [200.0, 400.0], [], None))
    asyncio.run(service.export_clips_by_id("sess-1"))
    cuts_after_plan_a = len(media.crops)
    plan_a_dir = media.crops[0].parent
    jobs.repository.rows["job-1"]["status"] = "succeeded"

    asyncio.run(service.request_clip_export("sess-1", [100.0, 300.0, 500.0], [], None))
    asyncio.run(service.export_clips_by_id("sess-1"))

    clips = sessions.current["outputs"]["videoClips"]
    assert len(clips) == 4
    # Four new ranges, four new cuts: nothing was adopted from plan A.
    assert len(media.crops) - cuts_after_plan_a == 4
    plan_b_dirs = {Path(clip["absolutePath"]).parent for clip in clips}
    assert len(plan_b_dirs) == 1 and plan_b_dirs != {plan_a_dir}
    assert all(clip["planId"] == sessions.current["clipExport"]["planId"] for clip in clips)
    assert all(clip["url"].startswith(f"/media/clips/sess-1/{clip['planId']}/") for clip in clips)
    # The superseded plan's directory is gone: no child session referenced it.
    assert not plan_a_dir.exists()


def test_a_retried_export_still_adopts_its_own_finished_clips(tmp_path) -> None:
    """Idempotency within a plan is kept — only the cross-plan leak is closed."""
    service, sessions, _events, media, _jobs = build_clip_harness(tmp_path, fail_on_index=1)
    asyncio.run(service.request_clip_export("sess-1", [200.0, 400.0], [], None))
    try:
        asyncio.run(service.export_clips_by_id("sess-1"))
    except RuntimeError:
        pass
    assert len(media.crops) == 1

    media.fail_on_index = None
    asyncio.run(service.export_clips_by_id("sess-1"))
    assert len(media.crops) == 3
    assert sessions.current["clipExport"]["status"] == "completed"


# --- F2: a rename during an export survives the export's checkpoints --------


def test_a_clip_renamed_mid_export_keeps_its_name(tmp_path) -> None:
    service, sessions, _events, media, _jobs = build_clip_harness(tmp_path)
    asyncio.run(service.request_clip_export("sess-1", [200.0, 400.0], [], None))

    original_crop = media.crop_video_segment
    renamed = {"done": False}

    async def crop_then_user_renames(**kwargs):
        await original_crop(**kwargs)
        if not renamed["done"]:
            renamed["done"] = True
            clip_id = sessions.current["outputs"]["videoClips"][0]["id"]
            # A PATCH /clips/{id} arriving while the job is between checkpoints.
            await service.rename_clip("sess-1", clip_id, "Alice")

    media.crop_video_segment = crop_then_user_renames
    asyncio.run(service.export_clips_by_id("sess-1"))

    first = sessions.current["outputs"]["videoClips"][0]
    assert first["label"] == "Alice"
    assert first["isDraft"] is False and first["fileName"]
    assert sessions.current["clipExport"]["completed"] == 3


# --- F3: startup recovery patches the row instead of writing a projection ---


def test_assembling_recovery_keeps_the_session_document(tmp_path) -> None:
    """A session stuck in "assembling" with no upload record to resume is failed
    through ``update``: status and error move, everything else stays."""
    from app.services.async_upload_service import AsyncUploadService

    settings = Settings(root_dir=tmp_path, backend_root=tmp_path)
    repo = SessionRepository(OrmDatabase(tmp_path / "s.sqlite3"))
    sessions = SessionService(settings, repo)
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()
    service = AsyncUploadService(
        settings,
        UploadRepository(uploads_dir),
        sessions,
        storage=LocalObjectStorageService(settings),
        jobs=None,  # type: ignore[arg-type]
        media=None,  # type: ignore[arg-type]
        events=EventService(settings),
        rubric_assets=None,  # type: ignore[arg-type]
        videos=None,  # type: ignore[arg-type]
    )

    async def scenario() -> dict[str, Any]:
        await repo.write(
            {
                "id": "s1",
                "name": "Case",
                "status": "assembling",
                "createdAt": "2026-01-01T00:00:00Z",
                "files": {"video": {"originalName": "a.mp4"}, "caseStudy": {"originalName": "r.pdf"}},
                "upload": {"id": "u1"},
                "job": {"id": "j1"},
                "corpus": {"id": "c1"},
            }
        )
        await service.recover_stale_assembling_uploads()
        return await repo.read("s1")

    after = asyncio.run(scenario())
    assert after["status"] == "failed"
    assert "interrupted" in str(after["error"])
    for key in ("files", "upload", "job", "corpus"):
        assert after[key], f"{key} was wiped by recovery"
    assert "hasVideoClips" not in after


def test_assembling_recovery_resumes_an_upload_whose_parts_are_complete(tmp_path) -> None:
    """The parts are on disk and assembly is idempotent, so a restart resumes it
    rather than asking the user to send the file again."""
    from app.services.async_upload_service import AsyncUploadService

    settings = Settings(root_dir=tmp_path, backend_root=tmp_path)
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()
    upload_repo = UploadRepository(uploads_dir)
    resumed: list[tuple[str, bool]] = []

    class ResumeProbe(AsyncUploadService):
        async def _assemble_and_dispatch(self, upload_id: str, should_process: bool) -> None:  # type: ignore[override]
            resumed.append((upload_id, should_process))

    repo = SessionRepository(OrmDatabase(tmp_path / "s.sqlite3"))
    service = ResumeProbe(
        settings,
        upload_repo,
        SessionService(settings, repo),
        storage=LocalObjectStorageService(settings),
        jobs=None,  # type: ignore[arg-type]
        media=None,  # type: ignore[arg-type]
        events=EventService(settings),
        rubric_assets=None,  # type: ignore[arg-type]
        videos=None,  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        await upload_repo.write(
            {
                "id": "u1",
                "sessionId": "s1",
                "status": "assembling",
                "autoProcess": True,
                "files": [{"fileId": "f1", "kind": "video", "sizeBytes": 20, "parts": [{"partNumber": 1, "sizeBytes": 20}]}],
            }
        )
        await repo.write({"id": "s1", "name": "Case", "status": "assembling", "createdAt": "2026-01-01T00:00:00Z"})
        await service.recover_stale_assembling_uploads()
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert resumed == [("u1", True)]


# --- F4: a job that dies at the claim fails its session ----------------------


class _Sessions(SessionService):
    pass


def _queue(tmp_path, pipeline) -> tuple[JobQueueService, SessionService]:
    settings = Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        job_queue_backend="local",
        local_job_auto_start=True,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
    )
    sessions = SessionService(settings, SessionRepository(OrmDatabase(tmp_path / "s.sqlite3")))
    repository = JobRepository(OrmDatabase(tmp_path / "jobs.sqlite3"))

    class Storage:
        async def prepare_session_sources(self, session: dict[str, Any]) -> dict[str, Any]:
            return session

    service = JobQueueService(settings, repository, EventService(settings), sessions, Storage())
    service.bind_handlers(pipeline=pipeline, clips=object())
    return service, sessions


class _RecordingPipeline:
    def __init__(self) -> None:
        self.failed: list[str] = []

    async def process_session_by_id(self, _session_id: str, *, allow_processing: bool = False) -> dict[str, Any]:
        return {"ok": True}

    async def mark_session_failed(self, session_id: str, _error: Exception) -> None:
        self.failed.append(session_id)


def test_a_job_exhausted_at_claim_time_fails_its_session(tmp_path) -> None:
    """Three crashes mid-run leave the row "running" at the attempt cap. Boot
    recovery requeues it, the claim finds no attempts left and fails the job —
    and, now, the session with it."""
    pipeline = _RecordingPipeline()
    service, sessions = _queue(tmp_path, pipeline)

    async def scenario() -> dict[str, Any]:
        await service.repository.initialize()
        await sessions.write({"id": "sess-1", "name": "S", "status": "processing", "createdAt": "2026-01-01T00:00:00Z"})
        job = await service.enqueue("sess-1", "process_session", auto_start=False)
        job["status"] = "running"
        job["attempts"] = job["maxAttempts"]
        await service.repository.write(job)

        await service.startup(dispatch_queued=True, recover_interrupted=True)
        task = next(iter(service._tasks.values()), None)
        if task is not None:
            await task
        return await service.repository.read(str(job["id"]))

    final_job = asyncio.run(scenario())
    assert final_job["status"] == "failed"
    assert "Maximum retry attempts" in str(final_job["error"])
    assert pipeline.failed == ["sess-1"], "the exhausted job must fail its session"


def test_startup_fails_in_flight_sessions_that_no_job_will_ever_touch(tmp_path) -> None:
    """The inline-request path (POST /process) sets "processing" with no job row.
    After a restart nothing can move that session; the boot sweep hands it back."""
    pipeline = _RecordingPipeline()
    service, sessions = _queue(tmp_path, pipeline)

    async def scenario() -> list[str]:
        await service.repository.initialize()
        await sessions.write({"id": "orphan", "name": "O", "status": "processing", "createdAt": "2026-01-01T00:00:00Z"})
        await sessions.write({"id": "queued-with-job", "name": "Q", "status": "queued", "createdAt": "2026-01-01T00:00:01Z"})
        await sessions.write({"id": "done", "name": "D", "status": "completed", "createdAt": "2026-01-01T00:00:02Z"})
        await service.enqueue("queued-with-job", "process_session", auto_start=False)
        return await service.reconcile_orphaned_sessions()

    orphaned = asyncio.run(scenario())
    assert orphaned == ["orphan"]
    assert pipeline.failed == ["orphan"]


# --- F5: parts stored in parallel are all recorded ----------------------------


def test_parallel_parts_are_all_recorded(tmp_path) -> None:
    from app.services.async_upload_service import AsyncUploadService

    settings = Settings(root_dir=tmp_path, backend_root=tmp_path)
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()
    upload_repo = UploadRepository(uploads_dir)
    repo = SessionRepository(OrmDatabase(tmp_path / "s.sqlite3"))
    sessions = SessionService(settings, repo)
    service = AsyncUploadService(
        settings,
        upload_repo,
        sessions,
        storage=LocalObjectStorageService(settings),
        jobs=None,  # type: ignore[arg-type]
        media=None,  # type: ignore[arg-type]
        events=EventService(settings),
        rubric_assets=None,  # type: ignore[arg-type]
        videos=None,  # type: ignore[arg-type]
    )

    async def scenario() -> dict[str, Any]:
        await repo.write({"id": "s1", "name": "Case", "status": "waiting_for_upload", "createdAt": "2026-01-01T00:00:00Z"})
        await upload_repo.write(
            {
                "id": "u1",
                "sessionId": "s1",
                "status": "initiated",
                "expiresAt": "2099-01-01T00:00:00Z",
                "files": [{"fileId": "f1", "kind": "video", "sizeBytes": 80, "parts": []}],
            }
        )
        await asyncio.gather(*(service.put_part("u1", "f1", n, b"x" * 10) for n in range(1, 9)))
        return await upload_repo.read("u1")

    upload = asyncio.run(scenario())
    parts = upload["files"][0]["parts"]
    assert [part["partNumber"] for part in parts] == list(range(1, 9))
    assert upload["files"][0]["uploadedBytes"] == 80
