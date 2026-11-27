"""``list_result_rows`` selects only the columns an analytics row returns.

The page reads one flat row per scored result; the full ``payload`` / ``payload_json``
documents of the session, assessment and result rows (the score sheets) are
never part of a row, so they must not be loaded to build it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import event, func, select

from app.core.utils import utc_iso
from app.database.models import (
    AssessmentResultRecord,
    AssessmentSessionRecord,
    SessionRecord,
    StudentRecord,
)
from app.database.orm import OrmDatabase
from app.repositories.assessment_repository import AssessmentRepository
from app.repositories.session_repository import SessionRepository
from app.services.assessment_service import AssessmentService


def _scored(session_id: str, name: str, **fields: Any) -> dict:
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
                    "padding": "x" * 2000,
                },
            },
        },
        **fields,
    }


def _legacy_root_fields(assessment: AssessmentSessionRecord, root: SessionRecord | None) -> dict[str, Any]:
    is_child = bool(assessment.parent_session_id)
    if root is not None:
        name, created_at = root.name, root.created_at
    else:
        name = None if is_child else assessment.session_name
        created_at = None if is_child else assessment.created_at
    return {
        "rootSessionId": assessment.parent_session_id or assessment.id,
        "rootSessionName": name,
        "rootSessionCreatedAt": utc_iso(created_at) if created_at else None,
    }


async def _legacy_rows(repository: AssessmentRepository) -> list[dict[str, Any]]:
    """The implementation this projection replaced: whole ORM entities."""
    root_id = func.coalesce(AssessmentSessionRecord.parent_session_id, AssessmentSessionRecord.id)
    async with repository.database.session() as db_session:
        rows = await db_session.execute(
            select(AssessmentResultRecord, AssessmentSessionRecord, StudentRecord, SessionRecord)
            .join(AssessmentSessionRecord, AssessmentResultRecord.assessment_session_id == AssessmentSessionRecord.id)
            .join(StudentRecord, AssessmentSessionRecord.student_id == StudentRecord.id)
            .outerjoin(SessionRecord, SessionRecord.id == root_id)
            .order_by(AssessmentSessionRecord.created_at.desc())
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
                "createdAt": utc_iso(assessment.created_at) if assessment.created_at else None,
                **_legacy_root_fields(assessment, root),
            }
            for result, assessment, student, root in rows.all()
        ]


def test_projection_matches_the_entity_query_and_loads_no_payload(tmp_path) -> None:
    async def _run() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        sessions = SessionRepository(database)
        repository = AssessmentRepository(database)
        service = AssessmentService(repository)

        await sessions.write(
            {"id": "recording-1", "name": "Constipation run 1", "status": "cropped", "workflow": "long", "outputs": {}}
        )
        for index in (1, 2):
            child = _scored(
                f"child-{index}",
                f"Constipation run 1 - Clip {index}",
                parentSessionId="recording-1",
                clipSource={"clipId": f"clip-{index}", "label": f"Clip {index}", "workflow": "long"},
            )
            await sessions.write(child)
            await service.record_session_results(child)
        standard = _scored("standard-1", "Asthma run 3", workflow="standard")
        await sessions.write(standard)
        await service.record_session_results(standard)
        await sessions.write({**await sessions.read("standard-1"), "name": "Asthma run 3 (renamed)"})
        # Orphaned child (parent row gone) and a session with no scores at all.
        await service.record_session_results(_scored("orphan-1", "Gone - Clip 1", parentSessionId="recording-gone"))
        await sessions.write({"id": "unscored-1", "name": "Never scored", "status": "completed", "outputs": {}})
        await service.record_session_results({"id": "unscored-1", "name": "Never scored", "status": "completed", "outputs": {}})

        statements: list[str] = []

        def _capture(conn, cursor, statement, *args) -> None:
            statements.append(statement)

        event.listen(database.engine.sync_engine, "before_cursor_execute", _capture)
        rows = await repository.list_result_rows()
        event.remove(database.engine.sync_engine, "before_cursor_execute", _capture)

        assert rows, "fixture produced no analytics rows"
        assert rows == await _legacy_rows(repository)
        by_id = {row["sessionId"]: row for row in rows}
        assert by_id["child-1"]["rootSessionName"] == "Constipation run 1"
        assert by_id["standard-1"]["rootSessionName"] == "Asthma run 3 (renamed)"
        assert by_id["orphan-1"]["rootSessionName"] is None

        select_sql = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        assert select_sql, "no SELECT captured"
        for sql in select_sql:
            assert "payload" not in sql.lower(), sql
        await database.shutdown()

    asyncio.run(_run())
