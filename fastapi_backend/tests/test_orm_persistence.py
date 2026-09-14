from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.database.models import AssessmentCriterionRecord, AssessmentResultRecord, AssessmentSessionRecord
from app.database.orm import OrmDatabase
from app.repositories.assessment_repository import AssessmentRepository
from app.repositories.rubric_asset_repository import RubricAssetRepository
from app.repositories.session_repository import SessionRepository
from app.services.assessment_service import AssessmentService
from app.services.rubric_asset_service import RubricAssetService


def test_rubric_asset_service_reuses_existing_matching_file(tmp_path) -> None:
    """Two uploads of the same PDF resolve to one stored asset.

    Exercised through ``register_case_study_storage_ref`` — the only case-study
    registration path there is, now that the single-shot ``POST /api/upload``
    route and its ``register_case_study_meta`` twin are gone.
    """

    async def _run() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        service = RubricAssetService(RubricAssetRepository(database))
        first_path = tmp_path / "first-upload.pdf"
        second_path = tmp_path / "second-upload.pdf"
        first_path.write_bytes(b"%PDF-1.4 same rubric")
        second_path.write_bytes(b"%PDF-1.4 same rubric")

        async def register(path):
            return await service.register_case_study_storage_ref(
                storage_ref={
                    "provider": "local",
                    "key": f"sessions/x/source/caseStudy/{path.name}",
                    "localPath": str(path),
                    "sizeBytes": path.stat().st_size,
                    "mimeType": "application/pdf",
                },
                original_name="PHR1012 rubric.pdf",
                safe_name=path.name,
                public_url=f"/media/source/{path.name}",
            )

        _, first_asset, first_duplicate = await register(first_path)
        second_ref, second_asset, second_duplicate = await register(second_path)

        assert first_duplicate is False
        assert second_duplicate is True
        assert second_asset["id"] == first_asset["id"]
        assert second_asset["absolutePath"] == str(first_path)
        # The canonical ref points at the surviving copy, not the one just
        # deleted, or a later run would materialise a path with no file.
        assert second_ref["localPath"] == str(first_path)
        assert first_path.exists()
        assert not second_path.exists()
        await database.shutdown()

    asyncio.run(_run())


def test_assessment_service_persists_session_results(tmp_path) -> None:
    async def _run() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        service = AssessmentService(AssessmentRepository(database))
        session = {
            "id": "session-1",
            "name": "Student A",
            "status": "completed",
            "workflow": "standard",
            "files": {
                "video": {"fileName": "student-a.mp4"},
                "caseStudy": {"fileName": "rubric.pdf"},
            },
            "outputs": {
                "scores": {
                    "absolutePath": str(tmp_path / "scores.json"),
                    "payload": {
                        # Real content-scorer summary shape (compute_scoring_summary in
                        # scripts/nvidia_osce_assessor.py): no total_score/max_score keys.
                        "scoring_summary": {
                            "total_criteria": 3,
                            "yes_count": 2,
                            "no_count": 1,
                            "pass_fail": "Pass",
                        },
                        # Real content-scorer criterion shape: Yes/No verdict in
                        # "value", justification in "reason".
                        "criteria": [
                            {
                                "label": "Confirm patient identity",
                                "value": "Yes",
                                "is_critical": True,
                                "timestamp": "00:00:12",
                                "reason": "Student asked for the patient's full name.",
                            }
                        ],
                    },
                },
                "communicationScores": {
                    "absolutePath": str(tmp_path / "communication_scores.json"),
                    "payload": {
                        # Real communication-scorer shape: per-criterion max only
                        # exists as the top-level scoring_scale.
                        "scoring_scale": {"All": 3, "Most": 2, "Some": 1, "None": 0},
                        "scoring_summary": {
                            "total_score": 9,
                            "max_score": 21,
                            "pass_fail": "Fail",
                        },
                        "criteria": [
                            {
                                "id": 1,
                                "label": "Builds rapport",
                                "score_label": "Some",
                                "points": 1,
                                "timestamp": "00:05:55",
                            }
                        ],
                    },
                },
            },
        }

        await service.record_session_results(session)

        async with database.session() as db_session:
            assessment = await db_session.get(AssessmentSessionRecord, "session-1")
            results = (await db_session.scalars(select(AssessmentResultRecord))).all()
            criteria = (await db_session.scalars(select(AssessmentCriterionRecord))).all()

        assert assessment is not None
        assert assessment.session_name == "Student A"
        by_type = {r.result_type: r for r in results}
        content = by_type["content"]
        assert content.score_total == 2
        assert content.score_max == 3
        assert content.pass_fail == "Pass"
        communication = by_type["communication"]
        assert communication.score_total == 9
        assert communication.score_max == 21
        assert communication.pass_fail == "Fail"

        by_result = {c.result_id: c for c in criteria}
        content_criterion = by_result[content.id]
        assert content_criterion.label == "Confirm patient identity"
        assert content_criterion.passed is True
        assert content_criterion.is_critical is True
        assert content_criterion.evidence == "Student asked for the patient's full name."
        # Yes/No checklist stored numerically so criterion rows aggregate uniformly.
        assert content_criterion.score == 1.0
        assert content_criterion.max_score == 1.0
        comm_criterion = by_result[communication.id]
        assert comm_criterion.score == 1.0
        # max_score propagated from the top-level scoring_scale.
        assert comm_criterion.max_score == 3.0
        assert comm_criterion.score_label == "Some"

        rows = await service.list_result_rows()
        assert len(rows) == 2
        by_row_type = {row["resultType"]: row for row in rows}
        content_row = by_row_type["content"]
        assert content_row["sessionId"] == "session-1"
        assert content_row["sessionName"] == "Student A"
        assert content_row["studentName"] == "Student A"
        assert content_row["scoreTotal"] == 2
        assert content_row["scoreMax"] == 3
        assert content_row["passFail"] == "Pass"
        assert content_row["createdAt"]
        assert by_row_type["communication"]["scoreTotal"] == 9
        await database.shutdown()

    asyncio.run(_run())


def _scored_session(session_id: str, name: str, **fields) -> dict:
    """A completed session with one content sheet — enough for one analytics row."""
    return {
        "id": session_id,
        "name": name,
        "status": "completed",
        "files": {"video": {"fileName": f"{session_id}.mp4"}, "caseStudy": {"fileName": "rubric.pdf"}},
        "outputs": {
            "scores": {
                "absolutePath": f"/nowhere/{session_id}.json",
                "payload": {
                    "scoring_summary": {"total_criteria": 2, "yes_count": 1, "no_count": 1, "pass_fail": "Fail"},
                    "criteria": [],
                },
            },
        },
        **fields,
    }


def test_analytics_rows_name_the_recording_they_belong_to(tmp_path) -> None:
    """Every analytics row carries its *root* session — the recording.

    A long recording is split into clips and each clip is scored as its own
    child session, so the assessment tables hold one row per clip and none
    for the recording itself. The analytics page groups its session filter by
    the recording, so the rows must say which one that is, by its live name
    from ``sessions`` — the only table that has it.
    """

    async def _run() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        sessions = SessionRepository(database)
        service = AssessmentService(AssessmentRepository(database))

        # The recording: cropped, never scored, so it has no assessment row.
        await sessions.write(
            {"id": "recording-1", "name": "Constipation run 1", "status": "cropped", "workflow": "long", "outputs": {}}
        )
        # Two clip children of it, each scored as its own student.
        for clip in ("Clip 1", "Clip 2"):
            child = _scored_session(
                f"child-{clip[-1]}",
                f"Constipation run 1 - {clip}",
                parentSessionId="recording-1",
                clipSource={"clipId": f"clip-{clip[-1]}", "label": clip, "workflow": "long"},
            )
            await sessions.write(child)
            await service.record_session_results(child)
        # A standard (short) session: it is its own recording, and its live
        # name has changed since it was scored.
        standard = _scored_session("standard-1", "Asthma run 3", workflow="standard")
        await sessions.write(standard)
        await service.record_session_results(standard)
        await sessions.write({**await sessions.read("standard-1"), "name": "Asthma run 3 (renamed)"})
        # A child whose parent row is gone: still reported, root unresolved.
        orphan = _scored_session("orphan-1", "Gone run - Clip 1", parentSessionId="recording-gone")
        await service.record_session_results(orphan)

        rows = {row["sessionId"]: row for row in await service.list_result_rows()}

        for child_id in ("child-1", "child-2"):
            assert rows[child_id]["rootSessionId"] == "recording-1"
            assert rows[child_id]["rootSessionName"] == "Constipation run 1"
            assert rows[child_id]["rootSessionCreatedAt"]
            # The scored session and its student are still the clip child.
            assert rows[child_id]["sessionId"] == child_id
            assert rows[child_id]["studentName"] == rows[child_id]["sessionName"]
        assert rows["child-1"]["studentId"] != rows["child-2"]["studentId"]

        assert rows["standard-1"]["rootSessionId"] == "standard-1"
        assert rows["standard-1"]["rootSessionName"] == "Asthma run 3 (renamed)"
        assert rows["standard-1"]["sessionName"] == "Asthma run 3"

        assert rows["orphan-1"]["rootSessionId"] == "recording-gone"
        assert rows["orphan-1"]["rootSessionName"] is None
        assert rows["orphan-1"]["rootSessionCreatedAt"] is None
        await database.shutdown()

    asyncio.run(_run())
