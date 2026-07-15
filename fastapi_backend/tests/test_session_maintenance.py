from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.exceptions import AppError
from app.services.container import create_container


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        app_database_url="",
        database_url="",
    )


async def _init(container) -> None:
    await container.artifacts.ensure_storage_layout()
    await container.storage.ensure_layout()
    await container.database.initialize()
    await container.orm_database.initialize()


def _score_payload() -> dict:
    return {
        "scoring_summary": {"total_criteria": 1, "yes_count": 1, "no_count": 0, "pass_fail": "Pass"},
        "criteria": [{"label": "Confirm identity", "value": "Yes", "is_critical": True}],
    }


def test_delete_session_cascades_children_and_wipes_data(tmp_path) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        await _init(container)

        # Files on disk: a parent video + shared case study, and one child's score output.
        parent_video = tmp_path / "parent-video.mp4"
        case_study = tmp_path / "shared-rubric.pdf"
        child_score = tmp_path / "child1-scores.json"
        for path in (parent_video, case_study, child_score):
            path.write_text("x", encoding="utf-8")

        parent_id = "parent-1"
        await container.sessions.write(
            {
                "id": parent_id,
                "name": "Long session",
                "status": "cropped",
                "workflow": "long",
                "files": {
                    "video": {"fileName": "parent.mp4", "absolutePath": str(parent_video)},
                    "caseStudy": {"fileName": "rubric.pdf", "absolutePath": str(case_study)},
                },
                "outputs": {"videoClips": [{"id": "clip-1", "label": "Student A"}]},
            }
        )

        child_id = "child-1"
        child_session = {
            "id": child_id,
            "name": "Student A",
            "status": "completed",
            "parentSessionId": parent_id,
            "clipSource": {"clipId": "clip-1", "label": "Student A"},
            "files": {
                # A child's "video" is the parent's exported clip — must NOT be deleted by the child.
                "video": {"fileName": "clip-1.mp4", "absolutePath": str(parent_video)},
                "caseStudy": {"fileName": "rubric.pdf", "absolutePath": str(case_study)},
            },
            "outputs": {"scores": {"absolutePath": str(child_score), "payload": _score_payload()}},
        }
        await container.sessions.write(child_session)
        # A second child with no extra rows, to prove multi-child cascade.
        await container.sessions.write(
            {
                "id": "child-2",
                "name": "Student B",
                "status": "failed",
                "parentSessionId": parent_id,
                "clipSource": {"clipId": "clip-2", "label": "Student B"},
                "outputs": {},
            }
        )

        # Assessment rows, a notification, a source-video row, and a job for child 1.
        await container.assessments.record_session_results(child_session)
        await container.notifications.notify("done", "scored", session_id=child_id)
        await container.videos.save(
            child_id,
            {"sizeBytes": 1, "mimeType": "video/mp4", "provider": "local", "localPath": str(parent_video)},
            original_name="clip-1.mp4",
            safe_name="clip-1.mp4",
        )
        await container.jobs.repository.write(
            {"id": "job-1", "sessionId": child_id, "taskType": "process_session", "status": "queued"}
        )

        assert len(await container.assessments.list_result_rows()) == 1

        result = await container.session_maintenance.delete_session(parent_id)

        assert set(result["deletedSessionIds"]) == {parent_id, child_id, "child-2"}
        for sid in (parent_id, child_id, "child-2"):
            with pytest.raises(FileNotFoundError):
                await container.sessions.read(sid)
        assert await container.assessments.list_result_rows() == []
        assert await container.notifications.list_rows() == []
        assert await container.videos.get_for_session(child_id) is None
        assert await container.jobs.list_jobs(child_id) == []
        # Owned artifacts gone; shared case study spared.
        assert not child_score.exists()
        assert not parent_video.exists()
        assert case_study.exists()

        await container.shutdown()

    asyncio.run(_run())


def test_rerun_session_keeps_id_and_reenqueues(tmp_path) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        await _init(container)

        # Don't actually run the pipeline — just verify state + enqueue.
        async def _noop_dispatch(_job) -> None:
            return None

        container.jobs._dispatch = _noop_dispatch  # type: ignore[method-assign]

        video = tmp_path / "clip.mp4"
        score = tmp_path / "scores.json"
        video.write_text("x", encoding="utf-8")
        score.write_text("x", encoding="utf-8")

        session_id = "child-rerun"
        session = {
            "id": session_id,
            "name": "Student A",
            "status": "completed",
            "parentSessionId": "parent-x",
            "clipSource": {"clipId": "clip-9", "label": "Student A"},
            "files": {"video": {"fileName": "clip.mp4", "absolutePath": str(video)}},
            "outputs": {"scores": {"absolutePath": str(score), "payload": _score_payload()}},
            "pipeline": {"startedAt": "2026-01-01T00:00:00Z"},
        }
        await container.sessions.write(session)
        await container.assessments.record_session_results(session)
        assert len(await container.assessments.list_result_rows()) == 1

        result = await container.session_maintenance.rerun_session(session_id)

        # Same identity, reset to a fresh queued run.
        assert result["session"]["id"] == session_id
        assert result["job"] is not None
        reloaded = await container.sessions.read(session_id)
        assert reloaded["status"] == "queued"
        assert (reloaded.get("outputs") or {}).get("scores") is None
        jobs = await container.jobs.list_jobs(session_id)
        assert [job["taskType"] for job in jobs] == ["process_session"]
        # Old scores + artifact cleared (re-created only when the fresh run finishes).
        assert await container.assessments.list_result_rows() == []
        assert not score.exists()
        # Source clip preserved so the re-run has something to process.
        assert video.exists()

        await container.shutdown()

    asyncio.run(_run())


def test_rerun_rejects_in_flight_session(tmp_path) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        await _init(container)

        session_id = "busy"
        await container.sessions.write({"id": session_id, "name": "Busy", "status": "processing", "outputs": {}})
        with pytest.raises(AppError) as excinfo:
            await container.session_maintenance.rerun_session(session_id)
        assert excinfo.value.status_code == 409

        await container.shutdown()

    asyncio.run(_run())
