"""Re-score: redo the chosen scoring branches of a session on the transcript it
already has, instead of ``rerun_session``'s delete-everything-and-transcribe.

Service behaviour first (what is kept, what is removed, how it refuses and how
it fails), then the pipeline reusing the kept artefacts, then the clip batch and
the routes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.core.exceptions import AppError
from app.domain.enums import ScoringBranch
from app.domain.enums import RescoreCode
from app.services.container import create_container
from app.services.session_maintenance_service import SessionMaintenanceService
from tests.fixtures.scoring_doubles import ContentMarkingSeam
from tests.test_routes import build_test_client
from tests.test_session_ownership import _owned_session, _second_marker_token
from tests.test_user_admin import GOOD_PASSWORD, MARKER_EMAIL, _activate_marker, _headers, _token

BOTH = frozenset({ScoringBranch.CONTENT, ScoringBranch.COMMUNICATION})
CONTENT = frozenset({ScoringBranch.CONTENT})
COMMUNICATION = frozenset({ScoringBranch.COMMUNICATION})

TRANSCRIPT = {
    "schema": "whisperx-segments-v1",
    "segments": [{"id": 1, "speaker": "SPEAKER_00", "start": 0.0, "end": 2.0, "text": "Good morning."}],
}
CONTENT_SHEET = {
    "scoring_summary": {"total_criteria": 1, "yes_count": 1, "no_count": 0, "pass_fail": "Pass"},
    "criteria": [{"label": "Confirm identity", "value": "Yes", "is_critical": True}],
}
COMMUNICATION_SHEET = {
    "scoring_summary": {"total_score": 3, "max_score": 3, "pass_fail": "Pass"},
    "scoring_scale": {"All": 3, "None": 0},
    "criteria": [{"label": "Empathy", "value": "All", "score": 3}],
}
AUDIO_SHEET = {"summary": {"overall": "ok"}}


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


def _hold_jobs(container) -> None:
    """Queue jobs without running them: these tests inspect the queued state."""

    async def _noop_dispatch(_job) -> None:
        return None

    container.jobs._dispatch = _noop_dispatch  # type: ignore[method-assign]


async def _container(tmp_path: Path):
    container = create_container(_settings(tmp_path))
    await container.artifacts.ensure_storage_layout()
    await container.storage.ensure_layout()
    await container.orm_database.initialize()
    _hold_jobs(container)
    return container


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _sidecar(path: Path) -> Path:
    return path.with_name(f".{path.name}.checkpoint.json")


class Files:
    """The on-disk artefacts of a fully scored standard session."""

    def __init__(self, container, session_id: str) -> None:
        paths = container.session_maintenance.settings.paths
        self.session_id = session_id
        self.transcript = _write_json(paths.output_transcripts_dir / f"{session_id}.json", TRANSCRIPT)
        self.audio = paths.output_audio_dir / f"{session_id}.mp3"
        self.audio.parent.mkdir(parents=True, exist_ok=True)
        self.audio.write_bytes(b"mp3")
        self.audio_professionalism = _write_json(
            paths.output_audio_professionalism_dir / f"{session_id}.json", AUDIO_SHEET,
        )
        self.whisperx_dir = paths.output_whisperx_dir / session_id
        self.whisperx_dir.mkdir(parents=True, exist_ok=True)
        (self.whisperx_dir / "raw.json").write_text("{}", encoding="utf-8")
        self.content = _write_json(paths.output_scores_dir / f"{session_id}.json", CONTENT_SHEET)
        self.content_checkpoint = _write_json(_sidecar(self.content), {})
        self.panel_dir = paths.output_scores_panel_dir / session_id
        _write_json(self.panel_dir / "marker-a.json", CONTENT_SHEET)
        _write_json(self.panel_dir / "adjudication.json", {})
        self.communication = _write_json(
            paths.output_communication_scores_dir / f"{session_id}.json", COMMUNICATION_SHEET,
        )
        self.communication_checkpoint = _write_json(_sidecar(self.communication), {})

    def output(self, path: Path) -> dict[str, Any]:
        return {"fileName": path.name, "absolutePath": str(path)}

    def outputs(self) -> dict[str, Any]:
        return {
            "audio": self.output(self.audio),
            "transcript": self.output(self.transcript),
            "audioProfessionalism": self.output(self.audio_professionalism),
            "scores": {**self.output(self.content), "panelArtifacts": {"adjudication": "/media/x"}},
            "communicationScores": self.output(self.communication),
        }


def _steps() -> dict[str, Any]:
    done = {"status": "completed", "updatedAt": "2026-01-01T00:00:00Z"}
    return {
        name: dict(done)
        for name in (
            "audio_extraction",
            "transcription",
            "transcript_normalization",
            "audio_professionalism",
            "communication_scoring",
            "content_scoring",
            "assessment_persistence",
        )
    }


async def _scored_session(container, session_id: str = "s1", **overrides: Any) -> tuple[dict[str, Any], Files]:
    files = Files(container, session_id)
    session = {
        "id": session_id,
        "name": f"Session {session_id}",
        "status": "completed",
        "workflow": "standard",
        "files": {"video": {"fileName": "v.mp4", "absolutePath": str(container.session_maintenance.settings.root_dir / "v.mp4")}},
        "outputs": files.outputs(),
        "pipeline": {"startedAt": "a", "endedAt": "b", "runtimeSeconds": 5, "mode": "cuda", "steps": _steps()},
        **overrides,
    }
    await container.sessions.write(session)
    await container.assessments.record_session_results(await container.sessions.read(session_id))
    return session, files


def _result_types(rows: list[dict[str, Any]]) -> set[str]:
    return {row["resultType"] for row in rows}


def run(coro_fn):
    asyncio.run(coro_fn())


# --- what is removed and what is kept -----------------------------------------------


def test_rescoring_both_branches_keeps_transcript_audio_and_audio_professionalism(tmp_path) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        _, files = await _scored_session(container)
        assert _result_types(await container.assessments.list_result_rows()) >= {"content", "communication"}

        result = await container.session_maintenance.rescore_session("s1", BOTH)

        assert result["job"]["taskType"] == "process_session"
        assert result["session"]["status"] == "queued"
        reloaded = await container.sessions.read("s1")
        assert set(reloaded["outputs"]) == {"audio", "transcript", "audioProfessionalism"}
        for kept in (files.transcript, files.audio, files.audio_professionalism, files.whisperx_dir / "raw.json"):
            assert kept.exists(), kept
        for removed in (files.content, files.content_checkpoint, files.communication, files.communication_checkpoint):
            assert not removed.exists(), removed
        assert not files.panel_dir.exists()
        steps = reloaded["pipeline"]["steps"]
        assert set(steps) == {"audio_extraction", "transcription", "transcript_normalization", "audio_professionalism"}
        assert reloaded["pipeline"]["startedAt"] is None and reloaded["pipeline"]["runtimeSeconds"] is None
        assert reloaded["error"] is None
        assert await container.assessments.list_result_rows() == []
        jobs = await container.jobs.list_jobs("s1")
        assert [job["taskType"] for job in jobs] == ["process_session"]
        assert jobs[0]["payload"]["rescore"] is True
        assert jobs[0]["payload"]["branches"] == ["communication", "content"]
        await container.shutdown()

    run(_run)


def test_rescoring_content_only_leaves_the_communication_branch_untouched(tmp_path) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        _, files = await _scored_session(container)

        await container.session_maintenance.rescore_session("s1", CONTENT)

        reloaded = await container.sessions.read("s1")
        assert set(reloaded["outputs"]) == {"audio", "transcript", "audioProfessionalism", "communicationScores"}
        assert files.communication.exists() and files.communication_checkpoint.exists()
        assert not files.content.exists() and not files.content_checkpoint.exists()
        # The panel artefacts are the content branch's alone.
        assert not files.panel_dir.exists()
        assert "communication_scoring" in reloaded["pipeline"]["steps"]
        assert "content_scoring" not in reloaded["pipeline"]["steps"]
        # Rows are dropped for both: the persistence step re-records every result.
        assert await container.assessments.list_result_rows() == []
        await container.shutdown()

    run(_run)


def test_rescoring_communication_only_keeps_content_and_its_panel_artefacts(tmp_path) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        _, files = await _scored_session(container)

        await container.session_maintenance.rescore_session("s1", COMMUNICATION)

        reloaded = await container.sessions.read("s1")
        assert "scores" in reloaded["outputs"] and "communicationScores" not in reloaded["outputs"]
        assert reloaded["outputs"]["scores"]["panelArtifacts"] == {"adjudication": "/media/x"}
        assert files.content.exists() and files.content_checkpoint.exists()
        assert (files.panel_dir / "adjudication.json").exists()
        assert not files.communication.exists() and not files.communication_checkpoint.exists()
        await container.shutdown()

    run(_run)


def test_a_sheet_on_disk_but_not_recorded_is_removed_too(tmp_path) -> None:
    """The pipeline adopts a well-formed sheet at the conventional path when the
    session records none, which would silently defeat the re-score."""

    async def _run() -> None:
        container = await _container(tmp_path)
        _, files = await _scored_session(container)
        session = await container.sessions.read("s1")
        outputs = dict(session["outputs"])
        outputs.pop("scores")
        outputs.pop("communicationScores")
        await container.sessions.update("s1", lambda current: current.update(outputs=outputs))

        await container.session_maintenance.rescore_session("s1", BOTH)

        assert not files.content.exists() and not files.communication.exists()
        await container.shutdown()

    run(_run)


def test_a_purged_source_video_does_not_block_a_rescore(tmp_path) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        await _scored_session(container, files={"video": {"fileName": "v.mp4", "absolutePath": None, "purgedAt": "2026-01-01T00:00:00Z"}})

        result = await container.session_maintenance.rescore_session("s1", BOTH)

        assert result["job"] is not None
        await container.shutdown()

    run(_run)


def test_a_clip_child_is_rescored_through_the_standard_pipeline(tmp_path) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        await _scored_session(container, parentSessionId="parent-1", clipSource={"clipId": "clip-1"})

        result = await container.session_maintenance.rescore_session("s1", BOTH)

        assert result["job"]["taskType"] == "process_session"
        await container.shutdown()

    run(_run)


# --- refusals ----------------------------------------------------------------------


def test_no_branches_is_refused_with_422(tmp_path) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        _, files = await _scored_session(container)
        with pytest.raises(AppError) as refused:
            await container.session_maintenance.rescore_session("s1", frozenset())
        assert refused.value.status_code == 422
        assert files.content.exists()
        await container.shutdown()

    run(_run)


@pytest.mark.parametrize("status", ["queued", "processing", "assembling"])
def test_an_in_flight_session_is_refused_and_nothing_is_deleted(tmp_path, status) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        _, files = await _scored_session(container, status=status)
        with pytest.raises(AppError) as refused:
            await container.session_maintenance.rescore_session("s1", BOTH)
        assert refused.value.status_code == 409
        assert files.content.exists() and files.communication.exists() and files.panel_dir.exists()
        assert (await container.sessions.read("s1"))["status"] == status
        assert len(await container.assessments.list_result_rows()) >= 2
        await container.shutdown()

    run(_run)


def test_a_long_recording_is_refused_and_pointed_at_its_clips(tmp_path) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        _, files = await _scored_session(container, workflow="long")
        with pytest.raises(AppError) as refused:
            await container.session_maintenance.rescore_session("s1", BOTH)
        assert refused.value.status_code == 409
        assert "clips" in refused.value.message.lower()
        assert files.content.exists()
        await container.shutdown()

    run(_run)


def test_a_session_with_no_transcript_output_is_refused_and_pointed_at_rerun(tmp_path) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        _, files = await _scored_session(container)
        await container.sessions.update("s1", lambda current: current["outputs"].pop("transcript"))
        with pytest.raises(AppError) as refused:
            await container.session_maintenance.rescore_session("s1", BOTH)
        assert refused.value.status_code == 409
        assert "re-run" in refused.value.message.lower()
        assert files.content.exists() and files.communication.exists()
        assert (await container.sessions.read("s1"))["status"] == "completed"
        await container.shutdown()

    run(_run)


def test_a_transcript_missing_from_disk_is_refused(tmp_path) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        _, files = await _scored_session(container)
        files.transcript.unlink()
        with pytest.raises(AppError) as refused:
            await container.session_maintenance.rescore_session("s1", BOTH)
        assert refused.value.status_code == 409
        assert files.content.exists()
        assert "scores" in (await container.sessions.read("s1"))["outputs"]
        await container.shutdown()

    run(_run)


def test_an_unknown_session_is_not_found(tmp_path) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        with pytest.raises(FileNotFoundError):
            await container.session_maintenance.rescore_session("missing", BOTH)
        await container.shutdown()

    run(_run)


# --- races and failures ------------------------------------------------------------


def test_two_concurrent_rescores_admit_exactly_one(tmp_path) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        await _scored_session(container)

        outcomes = await asyncio.gather(
            container.session_maintenance.rescore_session("s1", BOTH),
            container.session_maintenance.rescore_session("s1", CONTENT),
            return_exceptions=True,
        )

        admitted = [outcome for outcome in outcomes if isinstance(outcome, dict)]
        refused = [outcome for outcome in outcomes if isinstance(outcome, AppError)]
        assert len(admitted) == 1 and len(refused) == 1
        assert refused[0].status_code == 409
        assert len(await container.jobs.list_jobs("s1")) == 1
        await container.shutdown()

    run(_run)


def test_an_enqueue_failure_releases_the_admission(tmp_path) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        await _scored_session(container)

        async def _boom(*_args, **_kwargs):
            raise RuntimeError("queue down")

        container.jobs.enqueue = _boom  # type: ignore[method-assign]
        with pytest.raises(AppError) as failed:
            await container.session_maintenance.rescore_session("s1", BOTH)
        assert failed.value.status_code == 503

        reloaded = await container.sessions.read("s1")
        assert reloaded["status"] == "failed"
        # ...so the card offers Re-run and a second Re-score is admitted, not 409.
        del container.jobs.enqueue
        retry = await container.session_maintenance.rescore_session("s1", BOTH)
        assert retry["session"]["status"] == "queued"
        await container.shutdown()

    run(_run)


def test_an_assessment_row_delete_failure_releases_the_admission_and_keeps_the_sheets(tmp_path) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        _, files = await _scored_session(container)

        async def _boom(_session_id):
            raise RuntimeError("db down")

        container.assessments.delete_session_results = _boom  # type: ignore[method-assign]
        with pytest.raises(AppError) as failed:
            await container.session_maintenance.rescore_session("s1", BOTH)
        assert failed.value.status_code == 500 and failed.value.retryable

        assert (await container.sessions.read("s1"))["status"] == "failed"
        assert files.content.exists() and files.communication.exists() and files.panel_dir.exists()
        assert await container.jobs.list_jobs("s1") == []
        await container.shutdown()

    run(_run)


def test_a_sheet_that_survives_deletion_stops_the_rescore_instead_of_being_adopted(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        _, files = await _scored_session(container)
        real_unlink = SessionMaintenanceService._unlink

        def _locked(path_value) -> None:  # a locked file: the unlink silently fails
            if path_value and Path(str(path_value)) == files.communication:
                return
            real_unlink(path_value)

        monkeypatch.setattr(SessionMaintenanceService, "_unlink", staticmethod(_locked))
        with pytest.raises(AppError) as failed:
            await container.session_maintenance.rescore_session("s1", BOTH)
        assert failed.value.status_code == 500 and failed.value.retryable
        assert failed.value.message == "Could not clear this session's previous marks; try again."

        assert (await container.sessions.read("s1"))["status"] == "failed"
        assert files.communication.exists()
        assert await container.jobs.list_jobs("s1") == []
        assert await container.assessments.list_result_rows() == []  # rows go first, by design

        monkeypatch.undo()
        retry = await container.session_maintenance.rescore_session("s1", BOTH)
        assert retry["session"]["status"] == "queued"
        assert not files.communication.exists()
        await container.shutdown()

    run(_run)


def test_a_surviving_checkpoint_or_panel_directory_also_stops_it(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        _, files = await _scored_session(container)
        monkeypatch.setattr(SessionMaintenanceService, "_rmtree", staticmethod(lambda _path: None))
        with pytest.raises(AppError) as failed:
            await container.session_maintenance.rescore_session("s1", CONTENT)
        assert failed.value.status_code == 500
        assert files.panel_dir.exists()
        assert await container.jobs.list_jobs("s1") == []
        await container.shutdown()

    run(_run)


# --- the pipeline reuses what was kept ---------------------------------------------


class _FakeScoring(ContentMarkingSeam):
    """Writes real sheets so the run's outputs are recognisable; counts calls."""

    def __init__(self, container) -> None:
        self.paths = container.session_maintenance.settings.paths
        self.calls: list[str] = []

    async def run_audio_professionalism(self, session):
        self.calls.append("audio_professionalism")
        raise AssertionError("audio professionalism must be reused")

    async def run_communication_scoring(self, session, audio_professionalism):
        self.calls.append("communication")
        path = _write_json(self.paths.output_communication_scores_dir / f"{session['id']}.json", {**COMMUNICATION_SHEET, "fresh": True})
        return {"fileName": path.name, "absolutePath": str(path)}

    async def run_content_scoring(self, session):
        self.calls.append("content")
        path = _write_json(self.paths.output_scores_dir / f"{session['id']}.json", {**CONTENT_SHEET, "fresh": True})
        return {"fileName": path.name, "absolutePath": str(path)}


async def _run_queued_job(container, scoring: _FakeScoring, session_id: str = "s1") -> None:
    async def _no_transcription(_session):
        raise AssertionError("a re-score must not transcribe")

    container.pipeline.scoring = scoring
    container.pipeline._process_from_video = _no_transcription  # type: ignore[method-assign]
    await container.pipeline.process_session_by_id(session_id)


def test_the_run_regenerates_only_the_selected_branch_and_never_transcribes(tmp_path) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        _, files = await _scored_session(container)
        untouched = files.communication.read_text(encoding="utf-8")
        await container.session_maintenance.rescore_session("s1", CONTENT)

        scoring = _FakeScoring(container)
        await _run_queued_job(container, scoring)

        assert scoring.calls == ["content"]
        assert files.communication.read_text(encoding="utf-8") == untouched
        assert json.loads(files.content.read_text(encoding="utf-8"))["fresh"] is True
        finished = await container.sessions.read("s1")
        assert finished["status"] == "completed"
        assert set(finished["outputs"]) >= {"transcript", "audioProfessionalism", "scores", "communicationScores"}
        rows = await container.assessments.list_result_rows()
        assert _result_types(rows) >= {"content", "communication"}
        await container.shutdown()

    run(_run)


def test_rescoring_both_branches_runs_both_scorers_on_the_kept_transcript(tmp_path) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        await _scored_session(container)
        await container.session_maintenance.rescore_session("s1", BOTH)

        scoring = _FakeScoring(container)
        await _run_queued_job(container, scoring)

        assert sorted(scoring.calls) == ["communication", "content"]
        assert (await container.sessions.read("s1"))["status"] == "completed"
        await container.shutdown()

    run(_run)


# --- clips ---------------------------------------------------------------------------


async def _long_recording(container, clip_ids: list[str], *, parent_id: str = "parent") -> None:
    clips = [{"id": clip_id, "label": clip_id, "kind": "session", "absolutePath": None, "isDraft": True} for clip_id in clip_ids]
    clips.append({"id": "break", "label": "Break", "kind": "intermission"})
    await container.sessions.write(
        {
            "id": parent_id,
            "name": "Long recording",
            "status": "cropped",
            "workflow": "long",
            "outputs": {"videoClips": clips},
        }
    )


async def _child(container, clip_id: str, **overrides: Any) -> Files:
    child_id = f"child-{clip_id}"
    _, files = await _scored_session(
        container, child_id, parentSessionId="parent", clipSource={"clipId": clip_id}, **overrides,
    )
    return files


def test_rescore_clips_reports_each_child_and_never_fails_the_batch(tmp_path) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        await _long_recording(container, ["a", "b", "c", "d"])
        ok_files = await _child(container, "a")
        await _child(container, "b", status="processing")
        no_transcript = await _child(container, "c")
        no_transcript.transcript.unlink()
        # "d" was never assessed: no child at all.

        outcome = await container.session_maintenance.rescore_clips("parent", BOTH)

        by_clip = {item["clipId"]: item for item in outcome["results"]}
        assert list(by_clip) == ["a", "b", "c", "d"]  # clip order; the intermission is not a target
        assert by_clip["a"]["ok"] is True and by_clip["a"]["sessionId"] == "child-a"
        assert by_clip["a"]["job"]["taskType"] == "process_session"
        assert (by_clip["b"]["ok"], by_clip["b"]["statusCode"]) == (False, 409)
        assert (by_clip["c"]["ok"], by_clip["c"]["statusCode"]) == (False, 409)
        assert (by_clip["d"]["ok"], by_clip["d"]["statusCode"]) == (False, 409)
        assert {clip_id: item["code"] for clip_id, item in by_clip.items()} == {
            "a": RescoreCode.OK,
            "b": RescoreCode.IN_FLIGHT,
            "c": RescoreCode.NO_TRANSCRIPT,
            "d": RescoreCode.NOT_ASSESSED,
        }
        assert not ok_files.content.exists()
        assert no_transcript.content.exists()
        await container.shutdown()

    run(_run)


def test_rescore_clips_clip_ids_filter_and_unknown_ids(tmp_path) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        await _long_recording(container, ["a", "b"])
        await _child(container, "a")
        b_files = await _child(container, "b")

        outcome = await container.session_maintenance.rescore_clips("parent", CONTENT, ["b", "nope"])

        assert [(item["clipId"], item["ok"]) for item in outcome["results"]] == [("b", True), ("nope", False)]
        assert outcome["results"][1]["statusCode"] == 404
        assert [item["code"] for item in outcome["results"]] == [RescoreCode.OK, RescoreCode.NOT_FOUND]
        assert not b_files.content.exists()
        assert (await container.sessions.read("child-a"))["status"] == "completed"
        await container.shutdown()

    run(_run)


def test_rescore_clips_of_a_recording_with_no_children_is_empty_of_successes(tmp_path) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        await container.sessions.write(
            {"id": "parent", "name": "Fresh", "status": "cropped", "workflow": "long", "outputs": {}}
        )
        assert await container.session_maintenance.rescore_clips("parent", BOTH) == {"results": []}
        await container.shutdown()

    run(_run)


def test_rescore_clips_refuses_a_session_that_is_not_a_long_recording(tmp_path) -> None:
    async def _run() -> None:
        container = await _container(tmp_path)
        await _scored_session(container)
        with pytest.raises(AppError) as refused:
            await container.session_maintenance.rescore_clips("s1", BOTH)
        assert refused.value.status_code == 409
        with pytest.raises(AppError) as empty:
            await container.session_maintenance.rescore_clips("s1", frozenset())
        assert empty.value.status_code == 422
        with pytest.raises(FileNotFoundError):
            await container.session_maintenance.rescore_clips("missing", BOTH)
        await container.shutdown()

    run(_run)


# --- routes ----------------------------------------------------------------------------


def _admin_client(tmp_path):
    client = build_test_client(tmp_path)
    return client, _headers(_token(client))


async def _write_bare_sessions(container, *session_ids: str) -> None:
    await container.orm_database.initialize()
    for session_id in session_ids:
        await container.sessions.write({"id": session_id, "name": session_id, "status": "completed"})


def test_the_rescore_route_answers_202_with_the_session_and_job(tmp_path) -> None:
    client, headers = _admin_client(tmp_path)
    container = client.app.state.container

    async def _setup() -> None:
        await container.orm_database.initialize()
        _hold_jobs(container)
        await _scored_session(container)

    asyncio.run(_setup())

    response = client.post("/api/sessions/s1/rescore", json={"branches": ["content"]}, headers=headers)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["session"]["status"] == "queued"
    assert body["job"]["taskType"] == "process_session"


@pytest.mark.parametrize(
    "body",
    [{"branches": []}, {"branches": ["content", "content"]}, {"branches": ["audio"]}, {}, {"branches": "content"}],
)
def test_the_rescore_route_rejects_bad_branch_lists(tmp_path, body) -> None:
    client, headers = _admin_client(tmp_path)
    asyncio.run(_write_bare_sessions(client.app.state.container, "s1"))

    response = client.post("/api/sessions/s1/rescore", json=body, headers=headers)

    assert response.status_code == 400, response.text  # the app's schema-rejection status
    clips = client.post("/api/sessions/s1/clips/rescore", json=body, headers=headers)
    assert clips.status_code == 400, clips.text


def test_the_rescore_route_maps_a_refusal_and_a_missing_session(tmp_path) -> None:
    client, headers = _admin_client(tmp_path)
    container = client.app.state.container

    async def _setup() -> None:
        await container.orm_database.initialize()
        _hold_jobs(container)
        await _scored_session(container, status="processing")

    asyncio.run(_setup())

    assert client.post("/api/sessions/s1/rescore", json={"branches": ["content"]}, headers=headers).status_code == 409
    assert client.post("/api/sessions/nope/rescore", json={"branches": ["content"]}, headers=headers).status_code == 404


def test_the_clips_rescore_route_answers_results_and_rejects_an_empty_clip_list(tmp_path) -> None:
    client, headers = _admin_client(tmp_path)
    container = client.app.state.container

    async def _setup() -> None:
        await container.orm_database.initialize()
        _hold_jobs(container)
        await _long_recording(container, ["a"])
        await _child(container, "a")

    asyncio.run(_setup())

    response = client.post("/api/sessions/parent/clips/rescore", json={"branches": ["content"], "clipIds": ["a"]}, headers=headers)
    assert response.status_code == 202, response.text
    assert [(item["clipId"], item["ok"]) for item in response.json()["results"]] == [("a", True)]
    assert client.post(
        "/api/sessions/parent/clips/rescore", json={"branches": ["content"], "clipIds": []}, headers=headers,
    ).status_code == 400


def test_a_marker_cannot_rescore_another_markers_session(tmp_path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    _activate_marker(client, admin_token, MARKER_EMAIL)
    owner_token = _token(client, MARKER_EMAIL, GOOD_PASSWORD)
    other_token = _second_marker_token(client, admin_token)
    session_id = _owned_session(client, owner_token)
    parent = {"branches": ["content"]}

    for path in (f"/api/sessions/{session_id}/rescore", f"/api/sessions/{session_id}/clips/rescore"):
        forbidden = client.post(path, json=parent, headers=_headers(other_token))
        assert forbidden.status_code == 403, forbidden.text


def test_a_clip_batch_is_charged_once_against_the_hourly_budget(tmp_path) -> None:
    from app.core.rate_limit import FixedWindowRateLimiter

    client, headers = _admin_client(tmp_path)
    container = client.app.state.container
    container.expensive_operation_rate_limiter = FixedWindowRateLimiter(max_attempts=2, window_seconds=3600)

    async def _setup() -> None:
        await container.orm_database.initialize()
        _hold_jobs(container)
        await _long_recording(container, ["a", "b", "c"])
        for clip_id in ("a", "b", "c"):
            await _child(container, clip_id)

    asyncio.run(_setup())
    body = {"branches": ["communication"]}

    first = client.post("/api/sessions/parent/clips/rescore", json=body, headers=headers)
    assert first.status_code == 202 and all(item["ok"] for item in first.json()["results"])
    second = client.post("/api/sessions/parent/clips/rescore", json=body, headers=headers)
    assert second.status_code == 202  # still inside the budget of two: one unit per request, not per clip
    assert client.post("/api/sessions/parent/clips/rescore", json=body, headers=headers).status_code == 429
