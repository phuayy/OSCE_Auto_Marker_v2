from __future__ import annotations

import asyncio
import copy
import logging

from app.core.logging_utils import log_context
from app.services.pipeline_service import PipelineService
from tests.fixtures.session_store import SessionUpdateMixin


class _FakeSessions(SessionUpdateMixin):
    def __init__(self, session: dict | None = None) -> None:
        self.session = session
        self.writes = 0

    async def read(self, _session_id: str) -> dict:
        return copy.deepcopy(self.session or {})

    async def write(self, session: dict) -> None:
        self.writes += 1
        self.session = session


class _FakeEvents:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, dict | None]] = []

    async def publish(self, session_id: str, name: str, payload: dict | None = None) -> None:
        self.published.append((session_id, name, payload))


def test_log_context_has_correlation_keys() -> None:
    ctx = log_context("sess-1", "whisperx", runtime_seconds=1.5)
    assert ctx == {"trace_id": "sess-1", "stage": "whisperx", "runtime_seconds": 1.5}


def test_pipeline_step_completion_emits_structured_log(caplog) -> None:
    session = {"id": "sess-123", "pipeline": {}}
    service = PipelineService(_FakeSessions(session), events=None, media=None, scoring=None)

    with caplog.at_level(logging.INFO, logger="app.services.pipeline_service"):
        asyncio.run(service._mark_pipeline_step(session, "whisperx", "completed"))

    record = next(rec for rec in caplog.records if getattr(rec, "stage", None) == "whisperx" and rec.status == "completed")
    assert record.trace_id == "sess-123"
    assert hasattr(record, "runtime_seconds")


def test_pipeline_step_running_logs_start(caplog) -> None:
    """Step starts are logged so the pipeline's progress is visible in logs."""
    session = {"id": "sess-123", "pipeline": {}}
    service = PipelineService(_FakeSessions(session), events=None, media=None, scoring=None)

    with caplog.at_level(logging.INFO, logger="app.services.pipeline_service"):
        asyncio.run(service._mark_pipeline_step(session, "whisperx", "running"))

    record = next(rec for rec in caplog.records if getattr(rec, "stage", None) == "whisperx")
    assert record.status == "running"
    assert "started" in record.getMessage().lower()


def test_find_failed_step_identifies_most_recent_failure() -> None:
    session = {
        "pipeline": {
            "currentStep": None,
            "steps": {
                "whisperx": {"status": "completed", "updatedAt": "2026-01-01T00:00:01Z"},
                "content_scoring": {"status": "failed", "updatedAt": "2026-01-01T00:00:05Z"},
            },
        }
    }
    assert PipelineService._find_failed_step(session) == "content_scoring"


def test_find_failed_step_falls_back_to_current_step() -> None:
    session = {"pipeline": {"currentStep": "whisperx", "steps": {}}}
    assert PipelineService._find_failed_step(session) == "whisperx"


def test_mark_session_failed_logs_and_publishes_failed_step(caplog) -> None:
    session = {
        "id": "sess-9",
        "pipeline": {
            "startedAt": "2026-01-01T00:00:00Z",
            "steps": {"content_scoring": {"status": "failed", "updatedAt": "2026-01-01T00:00:05Z"}},
        },
    }
    sessions = _FakeSessions(session)
    events = _FakeEvents()
    service = PipelineService(sessions, events=events, media=None, scoring=None)

    with caplog.at_level(logging.ERROR, logger="app.services.pipeline_service"):
        asyncio.run(service.mark_session_failed("sess-9", RuntimeError("scorer exited 1")))

    record = next(rec for rec in caplog.records if "failed at step" in rec.getMessage())
    assert record.stage == "content_scoring"
    # The SSE status event carries the failed step for the client.
    assert any(payload and payload.get("failedStep") == "content_scoring" for _sid, _name, payload in events.published)
