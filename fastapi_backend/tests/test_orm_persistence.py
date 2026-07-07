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
    async def _run() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        service = RubricAssetService(RubricAssetRepository(database))
        first_path = tmp_path / "first-upload.pdf"
        second_path = tmp_path / "second-upload.pdf"
        first_path.write_bytes(b"%PDF-1.4 same rubric")
        second_path.write_bytes(b"%PDF-1.4 same rubric")

        first = await service.register_case_study_meta(
            {
                "absolutePath": str(first_path),
                "originalName": "PHR1012 rubric.pdf",
                "fileName": first_path.name,
                "sizeBytes": first_path.stat().st_size,
                "mimeType": "application/pdf",
            }
        )
        second = await service.register_case_study_meta(
            {
                "absolutePath": str(second_path),
                "originalName": "PHR1012 rubric.pdf",
                "fileName": second_path.name,
                "sizeBytes": second_path.stat().st_size,
                "mimeType": "application/pdf",
            }
        )

        assert second["rubricAssetId"] == first["rubricAssetId"]
        assert second["absolutePath"] == str(first_path)
        assert second["rubricDeduplicated"] is True
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
                        "scoring_summary": {
                            "total_score": 2,
                            "max_score": 3,
                            "pass_fail": "pass",
                        },
                        "criteria": [
                            {
                                "criterion": "Confirm patient identity",
                                "score": 1,
                                "max_score": 1,
                                "passed": True,
                                "timestamp": "00:00:12",
                            }
                        ],
                    },
                }
            },
        }

        await service.record_session_results(session)

        async with database.session() as db_session:
            assessment = await db_session.get(AssessmentSessionRecord, "session-1")
            result = await db_session.scalar(select(AssessmentResultRecord))
            criterion = await db_session.scalar(select(AssessmentCriterionRecord))

        assert assessment is not None
        assert assessment.session_name == "Student A"
        assert result is not None
        assert result.result_type == "content"
        assert result.score_total == 2
        assert criterion is not None
        assert criterion.label == "Confirm patient identity"
        assert criterion.passed is True
        await database.shutdown()

    asyncio.run(_run())
