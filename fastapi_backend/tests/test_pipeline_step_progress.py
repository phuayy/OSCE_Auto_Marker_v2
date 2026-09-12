"""Live step progress on the session record.

``pipeline.stepProgress`` is what the session-list projection exposes to the
cards' stage gauge, so its lifecycle matters as much as the value: it must
appear while a reporting step runs, never outlive that step, and never carry a
previous step's reading.
"""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

from app.services.pipeline_service import PipelineService

from tests.test_pipeline_service import FakeEvents, FakeMedia, FakeSessions, build_settings


def build_service(tmp_path: Path) -> tuple[PipelineService, FakeSessions]:
    settings = build_settings(tmp_path)
    sessions = FakeSessions()
    service = PipelineService(
        sessions=sessions,
        events=FakeEvents(),
        media=FakeMedia(settings),
        scoring=None,
    )
    return service, sessions


def running_session(service: PipelineService, step: str = "whisperx") -> dict[str, Any]:
    session: dict[str, Any] = {"id": "session-1"}
    service._set_pipeline_step_state(session, step, "running")
    # Progress is committed against the stored row (SessionService.update), so
    # the double has to hold the document the test is driving.
    service.sessions.current = copy.deepcopy(session)
    return session


def record(service: PipelineService, session: dict[str, Any], step: str, percent: float) -> None:
    asyncio.run(service._record_step_progress(session, step, percent, lock=asyncio.Lock()))


def test_recorded_progress_is_visible_to_the_list_projection(tmp_path: Path) -> None:
    service, sessions = build_service(tmp_path)
    session = running_session(service)

    record(service, session, "whisperx", 42.5)

    assert session["pipeline"]["stepProgress"] == 42.5
    assert session["pipeline"]["steps"]["whisperx"]["progress"] == 42.5
    # Persisted, not just mutated in memory — the cards read the database.
    assert sessions.writes[-1]["pipeline"]["stepProgress"] == 42.5


def test_a_running_step_starts_with_no_reading(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    session = running_session(service)

    assert session["pipeline"]["stepProgress"] is None
    assert "progress" not in session["pipeline"]["steps"]["whisperx"]


def test_completing_a_step_clears_its_progress(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    session = running_session(service)
    record(service, session, "whisperx", 90.0)

    service._set_pipeline_step_state(session, "whisperx", "completed")

    assert session["pipeline"]["stepProgress"] is None
    assert session["pipeline"]["currentStep"] is None


def test_failing_a_step_clears_its_progress(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    session = running_session(service)
    record(service, session, "whisperx", 30.0)

    service._set_pipeline_step_state(session, "whisperx", "failed", error=RuntimeError("boom"))

    assert session["pipeline"]["stepProgress"] is None


def test_the_next_step_does_not_inherit_the_previous_reading(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    session = running_session(service)
    record(service, session, "whisperx", 88.0)
    service._set_pipeline_step_state(session, "whisperx", "completed")

    service._set_pipeline_step_state(session, "transcript_normalization", "running")

    assert session["pipeline"]["currentStep"] == "transcript_normalization"
    assert session["pipeline"]["stepProgress"] is None


def test_progress_for_a_step_that_is_not_running_is_ignored(tmp_path: Path) -> None:
    # Subprocess output callbacks can land after the step ended.
    service, sessions = build_service(tmp_path)
    session = running_session(service)
    service._set_pipeline_step_state(session, "whisperx", "completed")
    sessions.current = copy.deepcopy(session)
    writes_before = len(sessions.writes)

    record(service, session, "whisperx", 88.0)

    assert session["pipeline"]["stepProgress"] is None
    assert "progress" not in session["pipeline"]["steps"]["whisperx"]
    assert len(sessions.writes) == writes_before


def test_progress_for_an_unknown_step_is_ignored(tmp_path: Path) -> None:
    service, sessions = build_service(tmp_path)
    session = running_session(service)
    writes_before = len(sessions.writes)

    record(service, session, "content_scoring", 50.0)

    assert session["pipeline"]["stepProgress"] is None
    assert len(sessions.writes) == writes_before


def test_successive_readings_overwrite_rather_than_accumulate(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    session = running_session(service)

    record(service, session, "whisperx", 10.0)
    record(service, session, "whisperx", 45.0)

    assert session["pipeline"]["stepProgress"] == 45.0


def test_concurrent_readings_are_serialised_by_the_lock(tmp_path: Path) -> None:
    # The real callers are output-reader threads dispatching onto the loop, so
    # several coroutines can be in flight over the same session document.
    service, _ = build_service(tmp_path)
    session = running_session(service)

    async def drive() -> None:
        lock = asyncio.Lock()
        await asyncio.gather(
            *(service._record_step_progress(session, "whisperx", value, lock=lock) for value in (5.0, 25.0, 45.0))
        )

    asyncio.run(drive())

    assert session["pipeline"]["stepProgress"] in {5.0, 25.0, 45.0}
    assert session["pipeline"]["steps"]["whisperx"]["status"] == "running"


# ---------------------------------------------------------------------------
# PARALLEL_SCORING: two branches running at once.
#
# The content and communication branches are started independently and run
# concurrently, so both steps can be "running" in `pipeline.steps` at the
# same time. currentStep/stepProgress must always describe the *content*
# branch (the higher-ranked, "more advanced" step per PIPELINE_STEP_ORDER)
# while it is running — never a mix of one branch's name and the other's
# percentage, and never a bare "Processing" the instant either branch ends
# while the other is still going.
# ---------------------------------------------------------------------------


def both_branches_running(service: PipelineService) -> dict[str, Any]:
    session: dict[str, Any] = {"id": "session-1"}
    service._set_pipeline_step_state(session, "communication_scoring", "running")
    service._set_pipeline_step_state(session, "content_scoring", "running")
    service.sessions.current = copy.deepcopy(session)
    return session


def test_communication_finishing_first_leaves_content_as_current(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    session = both_branches_running(service)
    record(service, session, "content_scoring", 40.0)

    service._set_pipeline_step_state(session, "communication_scoring", "completed")

    # The content branch's own reading survives the sibling branch ending —
    # it was never the one that finished.
    assert session["pipeline"]["currentStep"] == "content_scoring"
    assert session["pipeline"]["stepProgress"] == 40.0


def test_content_finishing_first_hands_current_to_communication(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    session = both_branches_running(service)
    record(service, session, "content_scoring", 90.0)

    service._set_pipeline_step_state(session, "content_scoring", "completed")

    # The bar must not drop to "Processing" while communication is still
    # running — it hands the label to the step that is demonstrably still
    # going, with no reading of its own yet.
    assert session["pipeline"]["currentStep"] == "communication_scoring"
    assert session["pipeline"]["stepProgress"] is None


def test_a_reading_recorded_for_the_non_current_branch_does_not_leak_into_stepprogress(
    tmp_path: Path,
) -> None:
    service, _ = build_service(tmp_path)
    session = both_branches_running(service)

    record(service, session, "communication_scoring", 65.0)

    # content_scoring outranks communication_scoring, so it stays current even
    # though it has not reported a percentage of its own yet.
    assert session["pipeline"]["currentStep"] == "content_scoring"
    assert session["pipeline"]["stepProgress"] is None
    assert session["pipeline"]["steps"]["communication_scoring"]["progress"] == 65.0
