from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.database.models import AssessmentCriterionRecord, AssessmentResultRecord, AssessmentSessionRecord
from app.database.orm import OrmDatabase
from app.repositories.assessment_repository import AssessmentRepository
from app.repositories.rubric_asset_repository import RubricAssetRepository
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
