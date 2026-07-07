from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AssessmentCriterionRecord,
    AssessmentResultRecord,
    AssessmentSessionRecord,
    ExaminerRecord,
    StudentRecord,
    utc_now,
)
from app.database.orm import OrmDatabase


class AssessmentRepository:
    def __init__(self, database: OrmDatabase) -> None:
        self.database = database

    async def upsert_assessment(
        self,
        *,
        session: dict[str, Any],
        results: list[dict[str, Any]],
    ) -> None:
        now = utc_now()
        async with self.database.transaction() as db_session:
            student = await self._get_or_create_student(db_session, session, now)
            examiner = await self._get_or_create_examiner(db_session, now)
            assessment = await self._get_or_create_assessment(db_session, session, student.id, now)
            self._update_assessment(assessment, session, student.id, now)

            for result_payload in results:
                await self._upsert_result(
                    db_session,
                    assessment.id,
                    examiner.id,
                    result_payload,
                    now,
                )

    async def _get_or_create_student(
        self,
        session: AsyncSession,
        payload: dict[str, Any],
        now,
    ) -> StudentRecord:
        external_id = str(payload.get("studentId") or payload.get("id"))
        display_name = str(payload.get("studentName") or payload.get("name") or external_id)
        student = await session.scalar(select(StudentRecord).where(StudentRecord.external_id == external_id))
        if student is None:
            student = StudentRecord(
                id=str(uuid4()),
                external_id=external_id,
                display_name=display_name,
                created_at=now,
                updated_at=now,
            )
            session.add(student)
        else:
            student.display_name = display_name
            student.updated_at = now
        await session.flush()
        return student

    async def _get_or_create_examiner(self, session: AsyncSession, now) -> ExaminerRecord:
        external_id = "ai-pipeline"
        examiner = await session.scalar(select(ExaminerRecord).where(ExaminerRecord.external_id == external_id))
        if examiner is None:
            examiner = ExaminerRecord(
                id=str(uuid4()),
                external_id=external_id,
                display_name="AI Pipeline",
                examiner_type="ai",
                created_at=now,
                updated_at=now,
            )
            session.add(examiner)
        else:
            examiner.updated_at = now
        await session.flush()
        return examiner

    async def _get_or_create_assessment(
        self,
        session: AsyncSession,
        payload: dict[str, Any],
        student_id: str,
        now,
    ) -> AssessmentSessionRecord:
        session_id = str(payload["id"])
        assessment = await session.get(AssessmentSessionRecord, session_id)
        if assessment is not None:
            return assessment
        assessment = AssessmentSessionRecord(
            id=session_id,
            student_id=student_id,
            status=str(payload.get("status") or "unknown"),
            created_at=now,
            updated_at=now,
        )
        session.add(assessment)
        await session.flush()
        return assessment

    @staticmethod
    def _update_assessment(
        assessment: AssessmentSessionRecord,
        payload: dict[str, Any],
        student_id: str,
        now,
    ) -> None:
        files = payload.get("files") or {}
        case_study = files.get("caseStudy") or {}
        video = files.get("video") or {}
        assessment.student_id = student_id
        assessment.case_study_rubric_id = case_study.get("rubricAssetId") or None
        assessment.communication_rubric_id = payload.get("communicationRubricAssetId") or None
        assessment.parent_session_id = payload.get("parentSessionId") or None
        assessment.workflow = payload.get("workflow") or payload.get("clipSource", {}).get("workflow")
        assessment.status = str(payload.get("status") or "unknown")
        assessment.session_name = payload.get("name") or None
        assessment.video_file_name = video.get("fileName") or video.get("originalName")
        assessment.case_study_file_name = case_study.get("fileName") or case_study.get("originalName")
        assessment.session_json_path = payload.get("sessionJsonPath") or None
        assessment.payload_json = {
            "pipeline": payload.get("pipeline") or {},
            "job": payload.get("job") or None,
            "upload": payload.get("upload") or None,
            "clipSource": payload.get("clipSource") or None,
        }
        assessment.updated_at = now

    async def _upsert_result(
        self,
        session: AsyncSession,
        assessment_session_id: str,
        examiner_id: str,
        payload: dict[str, Any],
        now,
    ) -> None:
        result_type = str(payload["resultType"])
        result = await session.scalar(
            select(AssessmentResultRecord).where(
                AssessmentResultRecord.assessment_session_id == assessment_session_id,
                AssessmentResultRecord.result_type == result_type,
            )
        )
        if result is None:
            result = AssessmentResultRecord(
                id=str(uuid4()),
                assessment_session_id=assessment_session_id,
                examiner_id=examiner_id,
                result_type=result_type,
                created_at=now,
                updated_at=now,
            )
            session.add(result)
            await session.flush()

        result.examiner_id = examiner_id
        result.status = str(payload.get("status") or "completed")
        result.score_total = self._float_or_none(payload.get("scoreTotal"))
        result.score_max = self._float_or_none(payload.get("scoreMax"))
        result.pass_fail = self._str_or_none(payload.get("passFail"))
        result.output_path = self._str_or_none(payload.get("outputPath"))
        result.payload_json = payload.get("payload") or {}
        result.updated_at = now
        await session.flush()

        await session.execute(delete(AssessmentCriterionRecord).where(AssessmentCriterionRecord.result_id == result.id))
        for index, criterion in enumerate(payload.get("criteria") or [], start=1):
            session.add(
                AssessmentCriterionRecord(
                    id=str(uuid4()),
                    result_id=result.id,
                    criterion_index=index,
                    criterion_key=self._str_or_none(criterion.get("criterionKey")),
                    label=self._str_or_none(criterion.get("label")),
                    score=self._float_or_none(criterion.get("score")),
                    max_score=self._float_or_none(criterion.get("maxScore")),
                    passed=self._bool_or_none(criterion.get("passed")),
                    is_critical=self._bool_or_none(criterion.get("isCritical")),
                    score_label=self._str_or_none(criterion.get("scoreLabel")),
                    timestamp=self._str_or_none(criterion.get("timestamp")),
                    evidence=self._str_or_none(criterion.get("evidence")),
                    payload_json=criterion.get("payload") or {},
                    created_at=now,
                )
            )

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _bool_or_none(value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        if value is None or value == "":
            return None
        token = str(value).strip().lower()
        if token in {"true", "yes", "y", "1", "pass", "passed"}:
            return True
        if token in {"false", "no", "n", "0", "fail", "failed"}:
            return False
        return None

    @staticmethod
    def _str_or_none(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None
