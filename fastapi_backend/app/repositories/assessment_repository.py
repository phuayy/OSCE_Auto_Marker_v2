from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AssessmentCriterionRecord,
    AssessmentResultRecord,
    AssessmentSessionRecord,
    ExaminerRecord,
    SessionRecord,
    StudentRecord,
    utc_now,
)
from app.database.orm import OrmDatabase
from app.domain.enums import AssessmentResultStatus


def _communication_rubric_id(payload: dict[str, Any]) -> str | None:
    """Which parsed communication rubric produced this session's marks.

    Recorded on the ``communicationScores`` output by the scoring step, so it
    is written in the same commit as the artefact it describes. Sessions
    written before that moved carry it at the top level; read both.
    """
    output = (payload.get("outputs") or {}).get("communicationScores")
    if isinstance(output, dict) and output.get("rubricAssetId"):
        return str(output["rubricAssetId"])
    legacy = payload.get("communicationRubricAssetId")
    return str(legacy) if legacy else None


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
        assessment.communication_rubric_id = _communication_rubric_id(payload)
        assessment.parent_session_id = payload.get("parentSessionId") or None
        assessment.workflow = payload.get("workflow") or (payload.get("clipSource") or {}).get("workflow")
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
        result.status = str(payload.get("status") or AssessmentResultStatus.COMPLETED)
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

    async def delete_for_session(self, session_id: str) -> bool:
        """Wipe every assessment row tied to a session: the criteria, the
        results, and the assessment_session itself. Deletes children before
        parents explicitly rather than leaning on ORM cascade (a bulk ``delete``
        statement bypasses relationship cascades), satisfying foreign-key
        enforcement on both SQLite and PostgreSQL without orphaned rows."""
        async with self.database.transaction() as db_session:
            assessment = await db_session.get(AssessmentSessionRecord, session_id)
            if assessment is None:
                return False
            result_ids = (
                await db_session.scalars(
                    select(AssessmentResultRecord.id).where(
                        AssessmentResultRecord.assessment_session_id == session_id
                    )
                )
            ).all()
            if result_ids:
                await db_session.execute(
                    delete(AssessmentCriterionRecord).where(
                        AssessmentCriterionRecord.result_id.in_(result_ids)
                    )
                )
            await db_session.execute(
                delete(AssessmentResultRecord).where(
                    AssessmentResultRecord.assessment_session_id == session_id
                )
            )
            await db_session.delete(assessment)
        return True

    # Analytics rows are read as one page for the client-side cross-tab
    # filtering CLAUDE.md documents ("Analytics filters are over recordings,
    # students, and the cascade between them") — a keyset cursor here, like
    # the session and job listings use, would break the very cohort
    # aggregation this page exists for. This cap is a backstop against
    # unbounded growth over a deployment's lifetime, not a pagination
    # contract: high enough that no real cohort should ever reach it, so a
    # deployment that somehow does keeps its most recent history rather than
    # the response growing without bound.
    MAX_RESULT_ROWS = 20_000

    async def list_result_rows(self) -> list[dict[str, Any]]:
        """Flat per-result rows joined with session + student, for analytics.

        Every row also names the *recording* it belongs to — ``rootSessionId``,
        ``rootSessionName``, ``rootSessionCreatedAt``. Two different things are
        called a session around an assessment: the *scored* session
        (``sessionId`` — a clip child for a long recording, the upload itself
        otherwise; one per student) and the recording that scored session was
        cut from. The recording of a long workflow is never scored itself, so
        it has no ``assessment_sessions`` row and its name lives only in
        ``sessions``; without this join the analytics page could only group by
        the child ids, and its session filter listed "Constipation run 1" once
        per clip instead of once.

        The root is resolved with a LEFT OUTER JOIN: a child whose parent row
        has since been deleted still comes back, with the root name unresolved
        rather than the row missing. For a standard session the root *is* the
        session, and its live ``sessions.name`` is preferred over the
        ``session_name`` snapshot taken at scoring time, so a rename after
        completion shows in the filter without a re-run.
        """
        root_id = func.coalesce(AssessmentSessionRecord.parent_session_id, AssessmentSessionRecord.id)
        async with self.database.session() as db_session:
            rows = await db_session.execute(
                select(AssessmentResultRecord, AssessmentSessionRecord, StudentRecord, SessionRecord)
                .join(
                    AssessmentSessionRecord,
                    AssessmentResultRecord.assessment_session_id == AssessmentSessionRecord.id,
                )
                .join(StudentRecord, AssessmentSessionRecord.student_id == StudentRecord.id)
                .outerjoin(SessionRecord, SessionRecord.id == root_id)
                .order_by(AssessmentSessionRecord.created_at.desc())
                .limit(self.MAX_RESULT_ROWS)
            )
            return [
                {
                    "sessionId": assessment.id,
                    "sessionName": assessment.session_name,
                    "studentId": student.id,
                    "studentName": student.display_name,
                    "resultType": result.result_type,
                    "status": result.status,
                    "scoreTotal": result.score_total,
                    "scoreMax": result.score_max,
                    "passFail": result.pass_fail,
                    "workflow": assessment.workflow,
                    "parentSessionId": assessment.parent_session_id,
                    "createdAt": assessment.created_at.isoformat() if assessment.created_at else None,
                    **self._root_session_fields(assessment, root),
                }
                for result, assessment, student, root in rows.all()
            ]

    @staticmethod
    def _root_session_fields(
        assessment: AssessmentSessionRecord,
        root: SessionRecord | None,
    ) -> dict[str, Any]:
        """The recording an assessment row belongs to, for grouping.

        ``root`` is the live ``sessions`` row for the parent (a clip child) or
        for the assessment's own id (a standard session); ``None`` when that
        row is gone. A standard session with no live row still has a name —
        the snapshot ``session_name`` — but a child with no parent row does
        not, and reporting the child's own name there would let one clip pose
        as a whole recording.
        """
        is_child = bool(assessment.parent_session_id)
        if root is not None:
            name = root.name
            created_at = root.created_at
        else:
            name = None if is_child else assessment.session_name
            created_at = None if is_child else assessment.created_at
        return {
            "rootSessionId": assessment.parent_session_id or assessment.id,
            "rootSessionName": name,
            "rootSessionCreatedAt": created_at.isoformat() if created_at else None,
        }

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
