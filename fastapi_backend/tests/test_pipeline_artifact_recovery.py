import asyncio
import json
from pathlib import Path

import pytest
from app.core.artifacts import artifact_metadata
from app.core.utils import write_text_atomic
from app.database.orm import OrmDatabase
from app.pipeline.scoring import ScoringPipeline
from app.repositories.assessment_repository import AssessmentRepository
from app.repositories.session_repository import SessionRepository
from app.services.assessment_service import AssessmentService
from app.services.pipeline_service import PipelineService
from app.services.session_service import SessionService

from tests.test_pipeline_service import (
    TRANSCRIPT_JSON,
    FakeEvents,
    FakeMedia,
    build_session,
    build_settings,
)

PAYLOAD = {
    "criteria": [
        {"id": 1, "is_critical": True, "value": "Yes", "timestamp": "00:00:01"},
        {"id": 2, "is_critical": False, "value": "Yes", "timestamp": "00:00:02"},
    ],
    "scoring_summary": {"total_criteria": 2, "critical_total": 1, "yes_count": 2, "pass_fail": "Pass"},
    "rubric_file": "embedded_in_case_study_pdf",
    "rubric_source": "simulation",
}


class FileScoring:
    should_refresh_score_payload = staticmethod(ScoringPipeline.should_refresh_score_payload)

    def __init__(self, path):
        self.path = path
        self.calls = 0

    async def run_content_scoring(self, session):
        self.calls += 1
        write_text_atomic(self.path, json.dumps(PAYLOAD))
        return artifact_metadata(self.path, "/media/scores")


class VanishingArtifactAssessment(AssessmentService):
    async def record_session_results(self, session):
        Path(session["outputs"]["scores"]["absolutePath"]).unlink()
        await super().record_session_results(session)


@pytest.mark.parametrize("corrupt", [False, True])
def test_scoring_cache_recovery_persists_real_rows_before_completion(tmp_path, corrupt):
    async def run():
        settings = build_settings(tmp_path, enable_audio_professionalism=False, enable_communication_scoring=False)
        database = OrmDatabase(tmp_path / "sessions.sqlite3")
        sessions = SessionService(settings, SessionRepository(database))
        assessments = AssessmentService(AssessmentRepository(database))
        scoring = FileScoring(tmp_path / "scores.json")
        pipeline = PipelineService(sessions, FakeEvents(), FakeMedia(settings), scoring, assessments=assessments)
        session = build_session(tmp_path)
        Path(session["outputs"]["transcript"]["absolutePath"]).write_text(TRANSCRIPT_JSON)
        session["outputs"]["scores"] = {"absolutePath": str(scoring.path)}
        if corrupt:
            scoring.path.write_text("{interrupted write")
        try:
            await sessions.write(session)
            result = await pipeline.process_session_by_id(session["id"])
            assert result["session"]["status"] == "completed"
            assert scoring.calls == 1
            assert len(await assessments.list_result_rows()) == 1
            persisted = await sessions.read(session["id"])
            assert "payload" not in persisted["outputs"]["scores"]
            await pipeline.process_session_by_id(session["id"])
            assert scoring.calls == 1
            assert len(await assessments.list_result_rows()) == 1
        finally:
            await database.shutdown()
    asyncio.run(run())


def test_artifact_lost_before_assessment_cannot_mark_session_completed(tmp_path):
    async def run():
        settings = build_settings(tmp_path, enable_audio_professionalism=False, enable_communication_scoring=False)
        database = OrmDatabase(tmp_path / "sessions.sqlite3")
        sessions = SessionService(settings, SessionRepository(database))
        assessments = VanishingArtifactAssessment(AssessmentRepository(database))
        pipeline = PipelineService(
            sessions, FakeEvents(), FakeMedia(settings), FileScoring(tmp_path / "scores.json"),
            assessments=assessments,
        )
        session = build_session(tmp_path)
        Path(session["outputs"]["transcript"]["absolutePath"]).write_text(TRANSCRIPT_JSON)
        try:
            await sessions.write(session)
            with pytest.raises(FileNotFoundError):
                await pipeline.process_session_by_id(session["id"])
            persisted = await sessions.read(session["id"])
            assert persisted["status"] != "completed"
            assert persisted["pipeline"]["steps"]["assessment_persistence"]["status"] == "failed"
            assert await assessments.list_result_rows() == []
            await pipeline.mark_session_failed(session["id"], FileNotFoundError("artifact lost"))
            pipeline.assessments = AssessmentService(AssessmentRepository(database))
            recovered = await pipeline.process_session_by_id(session["id"])
            assert recovered["session"]["status"] == "completed"
            assert len(await assessments.list_result_rows()) == 1
        finally:
            await database.shutdown()
    asyncio.run(run())
