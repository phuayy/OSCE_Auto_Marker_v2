"""The parallel scoring branches must not launder a permanent failure.

Bug: when one branch failed, the pipeline re-raised ``RuntimeError("Post-
transcription scoring failed: ...")``. The queue judges retryability from the
exception type, so a 422 (missing input) became a plain, retryable error and
burned every attempt.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.exceptions import AppError, is_retryable_failure
from app.services.pipeline_service import PipelineService
from tests.fixtures.events import RecordingEvents
from tests.fixtures.scoring_doubles import ContentMarkingSeam
from tests.test_pipeline_service import FakeMedia, FakeSessions, build_session, build_settings


def _run(tmp_path, *, communication: Exception | None, content: Exception | None) -> None:
    settings = build_settings(tmp_path, parallel_scoring=True)
    session = build_session(tmp_path)

    class Scoring(ContentMarkingSeam):
        should_refresh_audio_professionalism_payload = staticmethod(lambda _p: True)
        should_refresh_communication_payload = staticmethod(lambda _p: True)
        should_refresh_score_payload = staticmethod(lambda _p: True)

        async def run_audio_professionalism(self, _s: dict[str, Any]) -> dict[str, Any]:
            return {"absolutePath": "a.json", "payload": {"schema": "audio-professionalism-v1"}}

        async def run_communication_scoring(self, _s: dict[str, Any], _a: Any) -> dict[str, Any]:
            if isinstance(communication, BaseException):
                raise communication
            return {"absolutePath": "c.json", "payload": {}}

        async def run_content_scoring(self, _s: dict[str, Any]) -> dict[str, Any]:
            if isinstance(content, BaseException):
                raise content
            return {"absolutePath": "s.json", "payload": {}}

    service = PipelineService(
        sessions=FakeSessions(session), events=RecordingEvents(), media=FakeMedia(settings), scoring=Scoring()
    )
    asyncio.run(service._run_cached_scoring_branches(session))


def test_a_permanent_branch_error_is_raised_as_itself(tmp_path) -> None:
    missing = AppError("Case study not found: /x/case.pdf", status_code=422)
    with pytest.raises(AppError) as raised:
        _run(tmp_path, communication=None, content=missing)
    assert raised.value is missing
    assert not is_retryable_failure(raised.value)
    assert "Case study not found" in str(raised.value)


def test_two_transient_failures_stay_retryable(tmp_path) -> None:
    with pytest.raises(RuntimeError) as raised:
        _run(tmp_path, communication=RuntimeError("comm down"), content=RuntimeError("llm 503"))
    assert is_retryable_failure(raised.value)


def test_a_permanent_failure_decides_over_a_transient_one(tmp_path) -> None:
    permanent = AppError("bad rubric", status_code=422)
    with pytest.raises(AppError) as raised:
        _run(tmp_path, communication=permanent, content=RuntimeError("llm 503"))
    assert raised.value is permanent
    # ...whichever branch it came from.
    with pytest.raises(AppError) as raised:
        _run(tmp_path, communication=RuntimeError("comm down"), content=permanent)
    assert raised.value is permanent


def test_cancellation_propagates_unchanged(tmp_path) -> None:
    with pytest.raises(asyncio.CancelledError):
        _run(tmp_path, communication=None, content=asyncio.CancelledError())
