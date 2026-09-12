"""Regression tests for the communication rubric provenance fix.

``ScoringPipeline.run_communication_scoring`` used to set
``session["communicationRubricAssetId"]`` directly on the caller's in-memory
working dict. ``PipelineService._commit`` replaces that dict wholesale from
the stored row on the very next commit (see "Session write contract" in
CLAUDE.md), so the key never reached storage — and
``AssessmentRepository._update_assessment`` read it to fill
``assessment_sessions.communication_rubric_id``, which was therefore NULL for
every assessment.

The fix rides the id on the ``communicationScores`` output record instead:
``_refresh_or_load_output`` already persists whatever ``run()`` returns
through ``_assign_output`` inside ``_commit``, in the very same commit that
stores the artefact. These tests pin that the id (a) survives the commit that
stores the output, (b) is read back correctly by the repository, (c) falls
back to the legacy top-level field for sessions written before the move, and
(d) is simply absent — without breaking the scorer run — when the rubric
could not be parsed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.core.artifacts import artifact_metadata
from app.database.models import AssessmentSessionRecord
from app.database.orm import OrmDatabase
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
from tests.test_scoring_inputs import RecordingRunner, _flag, _pipeline

# A well-formed communication sheet: satisfies
# ScoringPipeline.should_refresh_communication_payload so it reads back as a
# genuine (non-stale) result, not just valid JSON.
COMMUNICATION_PAYLOAD = {
    "schema": "communication-scoring-v2",
    "criteria": [
        {"id": 1, "score_label": "Most"},
        {"id": 2, "score_label": "All"},
    ],
    "scoring_summary": {"total_score": 5, "max_score": 6, "pass_threshold": 4},
}


class CommunicationScoringDouble:
    """Stands in for ScoringPipeline.run_communication_scoring: writes a real
    sheet to disk and returns the artifact record carrying rubricAssetId,
    exactly like the fixed production code does."""

    def __init__(self, path: Path, rubric_asset_id: str | None) -> None:
        self.path = path
        self.rubric_asset_id = rubric_asset_id
        self.calls = 0

    async def run_communication_scoring(
        self, _session: dict[str, Any], _audio_professionalism: dict[str, Any] | None
    ) -> dict[str, Any]:
        self.calls += 1
        self.path.write_text(json.dumps(COMMUNICATION_PAYLOAD), encoding="utf-8")
        artifact = dict(artifact_metadata(self.path, "/media/communication-scores"))
        if self.rubric_asset_id:
            artifact["rubricAssetId"] = self.rubric_asset_id
        return artifact


def test_communication_rubric_id_survives_the_commit_that_stores_the_output(tmp_path: Path) -> None:
    async def run() -> None:
        settings = build_settings(tmp_path, enable_audio_professionalism=False, enable_scoring=False)
        database = OrmDatabase(tmp_path / "sessions.sqlite3")
        sessions = SessionService(settings, SessionRepository(database))
        assessments = AssessmentService(AssessmentRepository(database))
        scoring = CommunicationScoringDouble(tmp_path / "communication.json", "rubric-asset-1")
        pipeline = PipelineService(sessions, FakeEvents(), FakeMedia(settings), scoring, assessments=assessments)

        session = build_session(tmp_path)
        Path(session["outputs"]["transcript"]["absolutePath"]).write_text(TRANSCRIPT_JSON, encoding="utf-8")

        try:
            await sessions.write(session)
            result = await pipeline.process_session_by_id(session["id"])
            assert result["session"]["status"] == "completed"
            assert scoring.calls == 1

            # The regression: the in-memory working dict is not the evidence —
            # only a re-read of the stored row proves the id landed.
            stored = await sessions.read(session["id"])
            assert stored["outputs"]["communicationScores"]["rubricAssetId"] == "rubric-asset-1"

            async with database.transaction() as db_session:
                record = await db_session.get(AssessmentSessionRecord, session["id"])
                assert record is not None
                assert record.communication_rubric_id == "rubric-asset-1"
        finally:
            await database.shutdown()

    asyncio.run(run())


def test_assessment_repository_reads_the_rubric_id_from_the_communication_output(tmp_path: Path) -> None:
    async def run() -> None:
        database = OrmDatabase(tmp_path / "assessments.sqlite3")
        assessments = AssessmentService(AssessmentRepository(database))
        comm_path = tmp_path / "communication.json"
        comm_path.write_text(json.dumps(COMMUNICATION_PAYLOAD), encoding="utf-8")
        session = {
            "id": "session-with-output-rubric",
            "status": "completed",
            "outputs": {
                "communicationScores": {
                    "absolutePath": str(comm_path),
                    "rubricAssetId": "rubric-asset-42",
                },
            },
        }
        try:
            await assessments.record_session_results(session)
            async with database.transaction() as db_session:
                record = await db_session.get(AssessmentSessionRecord, session["id"])
                assert record is not None
                assert record.communication_rubric_id == "rubric-asset-42"
        finally:
            await database.shutdown()

    asyncio.run(run())


def test_legacy_top_level_communication_rubric_id_still_populates_the_column(tmp_path: Path) -> None:
    async def run() -> None:
        database = OrmDatabase(tmp_path / "assessments.sqlite3")
        assessments = AssessmentService(AssessmentRepository(database))
        comm_path = tmp_path / "communication.json"
        comm_path.write_text(json.dumps(COMMUNICATION_PAYLOAD), encoding="utf-8")
        session = {
            "id": "session-with-legacy-rubric",
            "status": "completed",
            "communicationRubricAssetId": "legacy-rubric-7",
            "outputs": {
                "communicationScores": {"absolutePath": str(comm_path)},
            },
        }
        try:
            await assessments.record_session_results(session)
            async with database.transaction() as db_session:
                record = await db_session.get(AssessmentSessionRecord, session["id"])
                assert record is not None
                assert record.communication_rubric_id == "legacy-rubric-7"
        finally:
            await database.shutdown()

    asyncio.run(run())


class _RubricService:
    def __init__(self, parsed: dict[str, Any] | None) -> None:
        self._parsed = parsed

    async def ensure_parsed(self) -> dict[str, Any] | None:
        return self._parsed


@pytest.mark.parametrize("parsed", [None, {}])
def test_no_rubric_id_recorded_when_the_rubric_cannot_be_parsed(tmp_path: Path, parsed: dict[str, Any] | None) -> None:
    transcript = tmp_path / "transcript.json"
    transcript.write_text(TRANSCRIPT_JSON, encoding="utf-8")

    def write_communication_scores(args: list[str]) -> None:
        output_path = Path(_flag(args, "--output"))
        # _pipeline() only creates output_scores_dir; communication_scores'
        # own directory is never pre-created for this fixture.
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(COMMUNICATION_PAYLOAD), encoding="utf-8")

    runner = RecordingRunner(write_communication_scores)
    pipeline = _pipeline(tmp_path, runner)
    pipeline.rubric_service = _RubricService(parsed)

    session = {
        "id": "s1",
        "outputs": {"transcript": {"absolutePath": str(transcript)}},
    }

    result = asyncio.run(pipeline.run_communication_scoring(session, None))

    assert "rubricAssetId" not in result, "an unparsed rubric must not fabricate provenance"
    assert len(runner.calls) == 1, "an unparsed rubric must not stop the communication scorer from running"
