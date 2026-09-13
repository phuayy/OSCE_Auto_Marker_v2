from __future__ import annotations

import asyncio
import json
import runpy
import sys
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.exceptions import AppError
from app.services.container import create_container
from app.services.session_maintenance_service import SessionMaintenanceService

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


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
    await container.orm_database.initialize()
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
        for path in (parent_video, case_study):
            path.write_text("x", encoding="utf-8")
        # A score artifact has to hold the sheet: `read_artifact_payload` takes
        # the file over any copy embedded on the session.
        child_score.write_text(json.dumps(_score_payload()), encoding="utf-8")

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
        score.write_text(json.dumps(_score_payload()), encoding="utf-8")

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


@pytest.mark.parametrize("workflow", ["standard", "long"])
def test_rerun_rejects_in_flight_session(tmp_path, workflow) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        await _init(container)

        session_id = "busy"
        await container.sessions.write(
            {"id": session_id, "name": "Busy", "status": "processing", "workflow": workflow, "outputs": {}}
        )
        with pytest.raises(AppError) as excinfo:
            await container.session_maintenance.rerun_session(session_id)
        assert excinfo.value.status_code == 409

        await container.shutdown()

    asyncio.run(_run())


def test_rerun_of_a_long_session_requeues_segmentation(tmp_path) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        await _init(container)

        async def _noop_dispatch(_job) -> None:
            return None

        container.jobs._dispatch = _noop_dispatch  # type: ignore[method-assign]

        video = tmp_path / "long-video.mp4"
        clip_file = tmp_path / "clip-1.mp4"
        video.write_text("x", encoding="utf-8")
        clip_file.write_text("x", encoding="utf-8")

        session_id = "long-1"
        session = {
            "id": session_id,
            "name": "Long session",
            "status": "failed",
            "workflow": "long",
            "files": {"video": {"fileName": "long-video.mp4", "absolutePath": str(video)}},
            "outputs": {"videoClips": [{"id": "clip-1", "label": "Student A", "absolutePath": str(clip_file)}]},
        }
        await container.sessions.write(session)

        result = await container.session_maintenance.rerun_session(session_id)

        assert result["job"]["taskType"] == "auto_crop"
        reloaded = await container.sessions.read(session_id)
        assert reloaded["status"] == "queued"
        jobs = await container.jobs.list_jobs(session_id)
        assert [job["taskType"] for job in jobs] == ["auto_crop"]

        await container.shutdown()

    asyncio.run(_run())


def test_rerun_of_a_long_session_keeps_its_clips_children_and_exported_files(tmp_path) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        await _init(container)

        async def _noop_dispatch(_job) -> None:
            return None

        container.jobs._dispatch = _noop_dispatch  # type: ignore[method-assign]

        video = tmp_path / "long-video.mp4"
        clip_file = tmp_path / "clip-1.mp4"
        video.write_text("x", encoding="utf-8")
        clip_file.write_text("x", encoding="utf-8")

        session_id = "long-2"
        session = {
            "id": session_id,
            "name": "Long session",
            "status": "failed",
            "workflow": "long",
            "files": {"video": {"fileName": "long-video.mp4", "absolutePath": str(video)}},
            "outputs": {"videoClips": [{"id": "clip-1", "label": "Student A", "absolutePath": str(clip_file)}]},
            "clipExport": {"planId": "plan-1", "status": "completed", "completed": 1, "total": 1},
        }
        await container.sessions.write(session)

        child_id = "child-of-long-2"
        child_session = {
            "id": child_id,
            "name": "Student A",
            "status": "completed",
            "parentSessionId": session_id,
            "clipSource": {"clipId": "clip-1", "label": "Student A"},
            "files": {"video": {"fileName": "clip-1.mp4", "absolutePath": str(clip_file)}},
            "outputs": {},
        }
        await container.sessions.write(child_session)

        await container.session_maintenance.rerun_session(session_id)

        reloaded = await container.sessions.read(session_id)
        clips = (reloaded.get("outputs") or {}).get("videoClips")
        assert isinstance(clips, list) and len(clips) == 1
        assert clips[0]["id"] == "clip-1"
        assert clip_file.exists()
        assert reloaded.get("clipExport") is None
        reloaded_child = await container.sessions.read(child_id)
        assert reloaded_child["id"] == child_id
        assert video.exists()

        await container.shutdown()

    asyncio.run(_run())


def test_rerun_of_a_long_session_clears_a_stray_pipeline_run_left_on_it(tmp_path) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        await _init(container)

        async def _noop_dispatch(_job) -> None:
            return None

        container.jobs._dispatch = _noop_dispatch  # type: ignore[method-assign]

        video = tmp_path / "long-video.mp4"
        clip_file = tmp_path / "clip-1.mp4"
        score_file = tmp_path / "scores.json"
        transcript_file = tmp_path / "transcript.json"
        for path in (video, clip_file, transcript_file):
            path.write_text("x", encoding="utf-8")
        score_file.write_text(json.dumps(_score_payload()), encoding="utf-8")

        session_id = "long-3"
        session = {
            "id": session_id,
            "name": "Long session",
            "status": "failed",
            "workflow": "long",
            "files": {"video": {"fileName": "long-video.mp4", "absolutePath": str(video)}},
            "outputs": {
                "videoClips": [{"id": "clip-1", "label": "Student A", "absolutePath": str(clip_file)}],
                "scores": {"absolutePath": str(score_file), "payload": _score_payload()},
                "transcript": {"absolutePath": str(transcript_file)},
            },
            "pipeline": {"startedAt": "2026-01-01T00:00:00Z"},
        }
        await container.sessions.write(session)

        await container.session_maintenance.rerun_session(session_id)

        reloaded = await container.sessions.read(session_id)
        outputs = reloaded.get("outputs") or {}
        assert outputs.get("scores") is None
        assert outputs.get("transcript") is None
        clips = outputs.get("videoClips")
        assert isinstance(clips, list) and len(clips) == 1
        assert not score_file.exists()
        assert not transcript_file.exists()
        assert clip_file.exists()
        assert reloaded["pipeline"]["startedAt"] is None

        await container.shutdown()

    asyncio.run(_run())


def test_rerun_of_a_standard_session_still_runs_the_pipeline_and_clears_outputs(tmp_path) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        await _init(container)

        async def _noop_dispatch(_job) -> None:
            return None

        container.jobs._dispatch = _noop_dispatch  # type: ignore[method-assign]

        video = tmp_path / "standard-video.mp4"
        score = tmp_path / "scores.json"
        video.write_text("x", encoding="utf-8")
        score.write_text(json.dumps(_score_payload()), encoding="utf-8")

        session_id = "standard-1"
        session = {
            "id": session_id,
            "name": "Standard session",
            "status": "failed",
            "workflow": "standard",
            "files": {"video": {"fileName": "standard-video.mp4", "absolutePath": str(video)}},
            "outputs": {"scores": {"absolutePath": str(score), "payload": _score_payload()}},
        }
        await container.sessions.write(session)
        await container.assessments.record_session_results(session)
        assert len(await container.assessments.list_result_rows()) == 1

        result = await container.session_maintenance.rerun_session(session_id)

        assert result["job"]["taskType"] == "process_session"
        reloaded = await container.sessions.read(session_id)
        assert reloaded["outputs"] == {}
        assert not score.exists()
        assert await container.assessments.list_result_rows() == []
        assert video.exists()

        await container.shutdown()

    asyncio.run(_run())


def test_rerun_of_a_clip_child_runs_the_standard_pipeline_even_when_its_parent_is_long(tmp_path) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        await _init(container)

        async def _noop_dispatch(_job) -> None:
            return None

        container.jobs._dispatch = _noop_dispatch  # type: ignore[method-assign]

        clip_file = tmp_path / "clip-1.mp4"
        clip_file.write_text("x", encoding="utf-8")

        session_id = "child-defensive-long"
        session = {
            "id": session_id,
            "name": "Student A",
            "status": "failed",
            "parentSessionId": "parent-long-1",
            "workflow": "long",  # defensive; a real child never carries this, but the
            # rule must hold even if one did
            "clipSource": {"clipId": "clip-1", "label": "Student A"},
            "files": {"video": {"fileName": "clip-1.mp4", "absolutePath": str(clip_file)}},
            "outputs": {},
        }
        await container.sessions.write(session)

        result = await container.session_maintenance.rerun_session(session_id)

        assert result["job"]["taskType"] == "process_session"
        jobs = await container.jobs.list_jobs(session_id)
        assert [job["taskType"] for job in jobs] == ["process_session"]

        await container.shutdown()

    asyncio.run(_run())


def test_delete_session_removes_the_panel_directory_and_the_checkpoint_sidecars(tmp_path) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        await _init(container)

        session_id = "panel-session-1"
        score_file = tmp_path / "storage" / "output" / "scores" / f"{session_id}.json"
        score_file.parent.mkdir(parents=True, exist_ok=True)
        score_file.write_text("x", encoding="utf-8")
        checkpoint = score_file.with_name(f".{score_file.name}.checkpoint.json")
        checkpoint.write_text("{}", encoding="utf-8")

        panel_dir = container.session_maintenance.settings.paths.output_scores_panel_dir / session_id
        panel_dir.mkdir(parents=True, exist_ok=True)
        (panel_dir / "nvidia__nemotron.json").write_text("{}", encoding="utf-8")
        (panel_dir / "adjudication.json").write_text("{}", encoding="utf-8")
        (panel_dir / ".nvidia__nemotron.json.checkpoint.json").write_text("{}", encoding="utf-8")

        # Another session's panel directory must survive this delete untouched.
        other_panel_dir = container.session_maintenance.settings.paths.output_scores_panel_dir / "other-session"
        other_panel_dir.mkdir(parents=True, exist_ok=True)
        (other_panel_dir / "nvidia__nemotron.json").write_text("{}", encoding="utf-8")

        await container.sessions.write(
            {
                "id": session_id,
                "name": "Panel session",
                "status": "completed",
                "outputs": {"scores": {"absolutePath": str(score_file), "payload": _score_payload()}},
            }
        )

        await container.session_maintenance.delete_session(session_id)

        assert not panel_dir.exists()
        assert not checkpoint.exists()
        assert not score_file.exists()
        assert other_panel_dir.exists()
        assert (other_panel_dir / "nvidia__nemotron.json").exists()

        await container.shutdown()

    asyncio.run(_run())


def test_rerun_removes_the_panel_directory_before_the_run_is_queued(tmp_path) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        await _init(container)

        async def _noop_dispatch(_job) -> None:
            return None

        container.jobs._dispatch = _noop_dispatch  # type: ignore[method-assign]

        video = tmp_path / "video.mp4"
        video.write_text("x", encoding="utf-8")

        session_id = "panel-rerun-1"
        panel_dir = container.session_maintenance.settings.paths.output_scores_panel_dir / session_id
        panel_dir.mkdir(parents=True, exist_ok=True)
        (panel_dir / "nvidia__nemotron.json").write_text("{}", encoding="utf-8")
        (panel_dir / "adjudication.json").write_text("{}", encoding="utf-8")

        await container.sessions.write(
            {
                "id": session_id,
                "name": "Panel rerun",
                "status": "completed",
                "workflow": "standard",
                "files": {"video": {"fileName": "video.mp4", "absolutePath": str(video)}},
                "outputs": {},
            }
        )

        result = await container.session_maintenance.rerun_session(session_id)

        assert not panel_dir.exists()
        assert result["job"] is not None
        reloaded = await container.sessions.read(session_id)
        assert reloaded["status"] == "queued"
        assert video.exists()

        await container.shutdown()

    asyncio.run(_run())


def test_the_checkpoint_sidecar_rule_matches_the_scorer_scripts(tmp_path, monkeypatch) -> None:
    """``SessionMaintenanceService._checkpoint_sidecar`` has to keep pace with
    ``scripts/scorer_checkpoint.checkpoint_path_for_output`` by hand, since the
    API cannot import ``scripts/`` (see the dependency direction note on
    ``_checkpoint_sidecar``). Pinned here so a change to one that is not
    mirrored on the other fails a test instead of leaking sidecar files."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("scorer_checkpoint", None)
    module = runpy.run_path(str(SCRIPTS / "scorer_checkpoint.py"))
    checkpoint_path_for_output = module["checkpoint_path_for_output"]

    output_path = tmp_path / "storage" / "output" / "scores" / "s1.json"
    assert SessionMaintenanceService._checkpoint_sidecar(str(output_path)) == str(
        checkpoint_path_for_output(output_path)
    )


def test_artifact_teardown_with_no_session_id_never_touches_a_shared_root(tmp_path) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        await _init(container)

        clips_root = container.session_maintenance.settings.paths.output_clips_dir
        panel_root = container.session_maintenance.settings.paths.output_scores_panel_dir
        clips_root.mkdir(parents=True, exist_ok=True)
        panel_root.mkdir(parents=True, exist_ok=True)
        (clips_root / "some-other-session-clip.mp4").write_text("x", encoding="utf-8")
        (panel_root / "some-other-session").mkdir(parents=True, exist_ok=True)

        container.session_maintenance._delete_artifacts({"outputs": {}}, keep_clips=False)

        assert clips_root.exists()
        assert (clips_root / "some-other-session-clip.mp4").exists()
        assert panel_root.exists()
        assert (panel_root / "some-other-session").exists()

        await container.shutdown()

    asyncio.run(_run())
