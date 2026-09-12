"""``sync_current_step`` / ``record_progress`` — the projection that keeps
``pipeline.currentStep``/``pipeline.stepProgress`` correct while more than one
pipeline step can be ``running`` at once.

``PARALLEL_SCORING`` runs the content and communication branches together, so
two steps can be ``running`` at the same time. Before this module existed,
each caller set ``currentStep``/``stepProgress`` by hand, and the two scalars
could disagree about which branch they were describing — the card could show
one branch's name next to the other branch's percentage, or drop to a bare
"Processing" the instant either branch finished. These tests pin the
single-helper contract that replaces that: ``currentStep`` is always the most
advanced *running* step, and ``stepProgress`` is always that step's own
reading.
"""
from __future__ import annotations

from typing import Any

from app.domain.enums import StepStatus
from app.domain.session_lifecycle import (
    PIPELINE_STEP_ORDER,
    record_progress,
    running_steps,
    step_rank,
    sync_current_step,
)


def running(step: str, *, progress: float | None = None) -> dict[str, Any]:
    state: dict[str, Any] = {"status": StepStatus.RUNNING}
    if progress is not None:
        state["progress"] = progress
    return state


def completed() -> dict[str, Any]:
    return {"status": StepStatus.COMPLETED}


# ---------------------------------------------------------------------------
# step_rank
# ---------------------------------------------------------------------------


def test_step_rank_orders_the_standard_pipeline() -> None:
    assert step_rank("audio_extraction") < step_rank("transcription")
    assert step_rank("transcription") < step_rank("communication_scoring")
    assert step_rank("communication_scoring") < step_rank("content_scoring")
    assert step_rank("content_scoring") < step_rank("assessment_persistence")


def test_whisperx_ranks_identically_to_transcription() -> None:
    # A session recorded before the transcription router shipped carries the
    # legacy "whisperx" key; it must sort exactly where "transcription" does,
    # so neither can spuriously outrank the other.
    assert step_rank("whisperx") == step_rank("transcription")


def test_segmentation_steps_rank_after_the_standard_pipeline() -> None:
    for step in PIPELINE_STEP_ORDER[: PIPELINE_STEP_ORDER.index("person_detection")]:
        assert step_rank(step) < step_rank("person_detection")
        assert step_rank(step) < step_rank("bell_detection")


def test_unknown_step_ranks_last_rather_than_raising() -> None:
    assert step_rank("some_future_step") > step_rank("bell_detection")


# ---------------------------------------------------------------------------
# running_steps
# ---------------------------------------------------------------------------


def test_running_steps_filters_and_sorts_by_rank() -> None:
    pipeline = {
        "steps": {
            "content_scoring": running("content_scoring"),
            "audio_extraction": completed(),
            "communication_scoring": running("communication_scoring"),
            "transcript_normalization": {"status": StepStatus.FAILED},
        }
    }

    assert running_steps(pipeline) == ["communication_scoring", "content_scoring"]


def test_running_steps_is_empty_for_no_steps_or_none_running() -> None:
    assert running_steps({}) == []
    assert running_steps({"steps": {}}) == []
    assert running_steps({"steps": {"audio_extraction": completed()}}) == []


# ---------------------------------------------------------------------------
# sync_current_step
# ---------------------------------------------------------------------------


def test_sync_picks_the_most_advanced_running_step() -> None:
    pipeline = {
        "steps": {
            "communication_scoring": running("communication_scoring", progress=70.0),
            "content_scoring": running("content_scoring", progress=40.0),
        }
    }

    sync_current_step(pipeline)

    assert pipeline["currentStep"] == "content_scoring"
    assert pipeline["stepProgress"] == 40.0


def test_completing_the_current_step_falls_back_to_the_other_running_one() -> None:
    pipeline = {
        "steps": {
            "communication_scoring": running("communication_scoring", progress=70.0),
            "content_scoring": completed(),
        }
    }

    sync_current_step(pipeline)

    assert pipeline["currentStep"] == "communication_scoring"
    assert pipeline["stepProgress"] == 70.0


def test_nothing_running_yields_none_and_none() -> None:
    pipeline = {"steps": {"audio_extraction": completed(), "transcription": completed()}}

    sync_current_step(pipeline)

    assert pipeline["currentStep"] is None
    assert pipeline["stepProgress"] is None


def test_a_running_step_with_no_reading_yet_projects_none_progress() -> None:
    pipeline = {"steps": {"transcription": running("transcription")}}

    sync_current_step(pipeline)

    assert pipeline["currentStep"] == "transcription"
    assert pipeline["stepProgress"] is None


def test_sync_overwrites_stale_scalars_left_by_a_hand_set() -> None:
    # Simulates a document that still carries scalars from before this module
    # owned them; sync must replace them wholesale, not merge with them.
    pipeline = {
        "currentStep": "communication_scoring",
        "stepProgress": 99.0,
        "steps": {"content_scoring": running("content_scoring", progress=12.0)},
    }

    sync_current_step(pipeline)

    assert pipeline["currentStep"] == "content_scoring"
    assert pipeline["stepProgress"] == 12.0


# ---------------------------------------------------------------------------
# record_progress
# ---------------------------------------------------------------------------


def test_record_progress_for_a_non_current_tracked_step_updates_only_its_own_slot() -> None:
    session = {
        "pipeline": {
            "currentStep": "content_scoring",
            "stepProgress": 40.0,
            "steps": {
                "communication_scoring": running("communication_scoring"),
                "content_scoring": running("content_scoring", progress=40.0),
            },
        }
    }

    record_progress(session, "communication_scoring", 65.0, tracked_step=True)

    pipeline = session["pipeline"]
    # The background branch's own reading is recorded...
    assert pipeline["steps"]["communication_scoring"]["progress"] == 65.0
    # ...but the scalar the card renders still belongs to the current step.
    assert pipeline["currentStep"] == "content_scoring"
    assert pipeline["stepProgress"] == 40.0


def test_record_progress_for_the_current_tracked_step_updates_both() -> None:
    session = {
        "pipeline": {
            "currentStep": "content_scoring",
            "stepProgress": None,
            "steps": {"content_scoring": running("content_scoring")},
        }
    }

    record_progress(session, "content_scoring", 55.0, tracked_step=True)

    pipeline = session["pipeline"]
    assert pipeline["steps"]["content_scoring"]["progress"] == 55.0
    assert pipeline["stepProgress"] == 55.0


def test_record_progress_for_a_step_that_is_not_running_is_ignored() -> None:
    session = {
        "pipeline": {
            "currentStep": "content_scoring",
            "steps": {"content_scoring": completed()},
        }
    }

    result = record_progress(session, "content_scoring", 90.0, tracked_step=True)

    assert result is False
    assert "progress" not in session["pipeline"]["steps"]["content_scoring"]


def test_record_progress_untracked_still_requires_being_the_current_step() -> None:
    # Segmentation steps are not tracked in `steps` at all; behaviour here must
    # stay exactly what it was before this module existed.
    session = {"pipeline": {"currentStep": "bell_detection"}}

    ignored = record_progress(session, "person_detection", 50.0, tracked_step=False)
    assert ignored is False
    assert "stepProgress" not in session["pipeline"]

    accepted = record_progress(session, "bell_detection", 50.0, tracked_step=False)
    assert accepted is None
    assert session["pipeline"]["stepProgress"] == 50.0
