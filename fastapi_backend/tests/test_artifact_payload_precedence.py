"""One rule for reading an artifact: the file on disk is the document.

``read_artifact_payload`` took a ``prefer_legacy`` flag, and exactly one caller
passed it — ``AssessmentService``, the thing that writes the rows analytics and
the results view are built from. So the reader that mattered most preferred an
inline copy embedded on the session over the file every other reader saw. A
session carrying a stale embedded payload (an old row, a fixture, a hand-edited
document) persisted the stale sheet's marks while the workspace rendered the
current one, with nothing anywhere saying they disagreed.

The flag is gone. The file wins; the embedded copy is the fallback for a record
whose file is not there.
"""

from __future__ import annotations

import asyncio
import json

from app.core.artifacts import read_artifact_payload
from app.database.orm import OrmDatabase
from app.repositories.assessment_repository import AssessmentRepository
from app.services.assessment_service import AssessmentService

CURRENT_SHEET = {
    "criteria": [{"id": 1, "is_critical": True, "value": "Yes"}],
    "scoring_summary": {"total_criteria": 1, "yes_count": 1, "pass_fail": "Pass"},
}
STALE_SHEET = {
    "criteria": [{"id": 1, "is_critical": True, "value": "No"}],
    "scoring_summary": {"total_criteria": 1, "yes_count": 0, "pass_fail": "Fail"},
}


def test_the_file_wins_over_an_embedded_copy(tmp_path) -> None:
    path = tmp_path / "scores.json"
    path.write_text(json.dumps(CURRENT_SHEET), encoding="utf-8")

    payload = asyncio.run(
        read_artifact_payload({"absolutePath": str(path), "payload": STALE_SHEET})
    )
    assert payload == CURRENT_SHEET


def test_an_embedded_copy_is_still_the_fallback_with_no_file(tmp_path) -> None:
    payload = asyncio.run(
        read_artifact_payload({"absolutePath": str(tmp_path / "gone.json"), "payload": STALE_SHEET})
    )
    assert payload == STALE_SHEET


def test_assessment_rows_are_built_from_the_file_not_the_embedded_copy(tmp_path) -> None:
    """The regression this closes: the persisted result used to be the stale one."""

    async def _run() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        await database.initialize()
        service = AssessmentService(AssessmentRepository(database))
        path = tmp_path / "scores.json"
        path.write_text(json.dumps(CURRENT_SHEET), encoding="utf-8")

        await service.record_session_results(
            {
                "id": "session-1",
                "name": "Student A",
                "status": "completed",
                "outputs": {"scores": {"absolutePath": str(path), "payload": STALE_SHEET}},
            }
        )

        rows = await service.list_result_rows()
        content = [row for row in rows if row.get("resultType") == "content"]
        assert len(content) == 1
        assert content[0]["passFail"] == "Pass"
        assert content[0]["scoreTotal"] == 1
        await database.shutdown()

    asyncio.run(_run())
