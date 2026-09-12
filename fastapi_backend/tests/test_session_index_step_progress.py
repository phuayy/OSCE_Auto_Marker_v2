"""``stepProgress`` on the session-list projection.

The cards poll the list projection rather than the full session document, so a
live WhisperX percentage only reaches the UI if this extracted field carries
it — as a number, from whichever JSON backend is in use.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.database.orm import OrmDatabase
from app.repositories.session_repository import SessionRepository


def make_repository(tmp_path: Path) -> SessionRepository:
    return SessionRepository(OrmDatabase(tmp_path / "sessions.sqlite3"))


def session_payload(session_id: str, pipeline: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": session_id,
        "name": session_id,
        "status": "processing",
        "workflow": "standard",
        "pipeline": pipeline,
        "outputs": {},
    }


def write_and_project(tmp_path: Path, pipeline: dict[str, Any]) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        repository = make_repository(tmp_path)
        await repository.database.initialize()
        await repository.write(session_payload("session-1", pipeline))
        projections = await repository.read_index_projection()
        return next(row for row in projections if row["id"] == "session-1")

    return asyncio.run(_run())


def test_running_step_progress_is_projected_as_a_number(tmp_path: Path) -> None:
    row = write_and_project(
        tmp_path,
        {"currentStep": "whisperx", "stepProgress": 42.5, "steps": {"whisperx": {"status": "running"}}},
    )

    assert row["currentStep"] == "whisperx"
    assert row["stepProgress"] == 42.5
    assert isinstance(row["stepProgress"], float)


def test_absent_progress_projects_as_none(tmp_path: Path) -> None:
    row = write_and_project(tmp_path, {"currentStep": "audio_extraction", "steps": {}})

    assert row["stepProgress"] is None


def test_cleared_progress_projects_as_none(tmp_path: Path) -> None:
    row = write_and_project(tmp_path, {"currentStep": None, "stepProgress": None, "steps": {}})

    assert row["stepProgress"] is None


def test_zero_progress_survives_the_projection(tmp_path: Path) -> None:
    # 0.0 is a real reading at the start of a step, not "no reading".
    row = write_and_project(
        tmp_path,
        {"currentStep": "whisperx", "stepProgress": 0.0, "steps": {"whisperx": {"status": "running"}}},
    )

    assert row["stepProgress"] == 0.0


def test_projection_keeps_the_fields_the_cards_already_use(tmp_path: Path) -> None:
    row = write_and_project(
        tmp_path,
        {"currentStep": "whisperx", "stepProgress": 10.0, "startedAt": "2026-01-01T00:00:00Z", "steps": {}},
    )

    assert row["status"] == "processing"
    assert row["workflow"] == "standard"
    assert row["pipelineStartedAt"] == "2026-01-01T00:00:00Z"
    assert row["hasVideoClips"] is False


# ---------------------------------------------------------------------------
# `steps`: the compact per-step map that lets the card gauge a parallel
# branch (PARALLEL_SCORING) independently of whichever step currentStep
# happens to name.
# ---------------------------------------------------------------------------


def test_steps_is_projected_as_a_compact_status_and_progress_map(tmp_path: Path) -> None:
    row = write_and_project(
        tmp_path,
        {
            "currentStep": "content_scoring",
            "stepProgress": 40.0,
            "steps": {
                "audio_extraction": {
                    "status": "completed",
                    "startedAt": "2026-01-01T00:00:00Z",
                    "endedAt": "2026-01-01T00:00:05Z",
                    "runtimeSeconds": 5.0,
                },
                "communication_scoring": {"status": "running"},
                "content_scoring": {"status": "running", "progress": 40.0},
            },
        },
    )

    # Both branches' own status/progress travel, independently of currentStep.
    assert row["steps"] == {
        "audio_extraction": {"status": "completed", "progress": None},
        "communication_scoring": {"status": "running", "progress": None},
        "content_scoring": {"status": "running", "progress": 40.0},
    }
    # Metadata/timestamps are dropped — the cards have no use for them and
    # shipping them would defeat the point of a lightweight list projection.
    assert "startedAt" not in row["steps"]["audio_extraction"]
    assert "runtimeSeconds" not in row["steps"]["audio_extraction"]


def test_steps_is_none_when_the_session_has_no_pipeline_steps(tmp_path: Path) -> None:
    row = write_and_project(tmp_path, {"currentStep": None, "stepProgress": None})

    assert row["steps"] is None


def test_steps_is_an_empty_dict_when_present_but_empty(tmp_path: Path) -> None:
    row = write_and_project(tmp_path, {"currentStep": None, "stepProgress": None, "steps": {}})

    assert row["steps"] == {}
