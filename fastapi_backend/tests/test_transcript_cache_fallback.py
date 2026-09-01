"""A recorded transcript path is not proof the artifact still exists.

``process_session_by_id`` used to branch on the *presence of the path string*,
so once the transcript file was removed from disk (manual cleanup, a wiped
storage volume, a partially-applied re-run) every subsequent run took the cached
branch and died reading a file that was not there — the session could never
recover. These tests pin the existence check and the dangling-reference cleanup
that let such a session fall back to full transcription.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.core.exceptions import AppError
from app.services.pipeline_service import PipelineService

from tests.test_pipeline_service import (
    TRANSCRIPT_JSON,
    FakeEvents,
    FakeMedia,
    FakeSessions,
    build_session,
    build_settings,
)


def build_service(tmp_path: Path, sessions: FakeSessions) -> PipelineService:
    settings = build_settings(
        tmp_path,
        enable_audio_professionalism=False,
        enable_communication_scoring=False,
        enable_scoring=False,
    )
    return PipelineService(
        sessions=sessions,
        events=FakeEvents(),
        media=FakeMedia(settings),
        scoring=object(),
    )


def session_with_transcript(tmp_path: Path, *, on_disk: bool) -> dict[str, Any]:
    transcript_path = tmp_path / "transcript.json"
    if on_disk:
        transcript_path.write_text(TRANSCRIPT_JSON, encoding="utf-8")
    session = build_session(tmp_path)
    session["outputs"]["transcript"] = {
        "fileName": transcript_path.name,
        "absolutePath": str(transcript_path),
        "sizeBytes": 128,
    }
    return session


def record_video_path(service: PipelineService) -> list[str]:
    """Replace the from-video path with a recorder, so a test can assert which
    branch was taken without standing up the whole media pipeline."""
    taken: list[str] = []

    async def _fake_process_from_video(session: dict[str, Any]) -> dict[str, Any]:
        taken.append(str(session["id"]))
        return {"session": session}

    service._process_from_video = _fake_process_from_video  # type: ignore[method-assign]
    return taken


def test_missing_transcript_artifact_falls_back_to_full_transcription(tmp_path: Path) -> None:
    session = session_with_transcript(tmp_path, on_disk=False)
    sessions = FakeSessions(session)
    service = build_service(tmp_path, sessions)
    taken = record_video_path(service)

    asyncio.run(service.process_session_by_id("session-1"))

    assert taken == ["session-1"]


def test_present_transcript_artifact_still_uses_the_cached_path(tmp_path: Path) -> None:
    session = session_with_transcript(tmp_path, on_disk=True)
    sessions = FakeSessions(session)
    service = build_service(tmp_path, sessions)
    taken = record_video_path(service)

    result = asyncio.run(service.process_session_by_id("session-1"))

    assert taken == []
    assert result["transcript"]["segments"][0]["text"] == "Good morning."


def test_dangling_transcript_reference_is_cleared_from_the_session(tmp_path: Path) -> None:
    """The stale reference is dropped so the session record stops advertising an
    artifact that is gone — otherwise the API keeps offering a dead download."""
    session = session_with_transcript(tmp_path, on_disk=False)

    assert PipelineService._has_cached_transcript_artifact(session) is False
    assert session["outputs"]["transcript"] is None


def test_transcript_reference_survives_when_the_file_exists(tmp_path: Path) -> None:
    session = session_with_transcript(tmp_path, on_disk=True)

    assert PipelineService._has_cached_transcript_artifact(session) is True
    assert session["outputs"]["transcript"]["absolutePath"].endswith("transcript.json")


def test_a_directory_at_the_transcript_path_is_not_a_usable_artifact(tmp_path: Path) -> None:
    session = build_session(tmp_path)
    directory = tmp_path / "transcript.json"
    directory.mkdir()
    session["outputs"]["transcript"] = {"absolutePath": str(directory)}

    assert PipelineService._has_cached_transcript_artifact(session) is False


def test_session_without_any_transcript_reference_uses_the_video_path(tmp_path: Path) -> None:
    session = build_session(tmp_path)
    session["outputs"]["transcript"] = None
    service = build_service(tmp_path, FakeSessions(session))
    taken = record_video_path(service)

    asyncio.run(service.process_session_by_id("session-1"))

    assert taken == ["session-1"]


def test_in_flight_guard_is_checked_before_the_artifact_lookup(tmp_path: Path) -> None:
    """A session another worker already claimed must be rejected regardless of
    which branch its artifacts would have selected."""
    session = session_with_transcript(tmp_path, on_disk=False)
    session["status"] = "processing"
    service = build_service(tmp_path, FakeSessions(session))
    taken = record_video_path(service)

    with pytest.raises(AppError, match="already being processed"):
        asyncio.run(service.process_session_by_id("session-1"))
    assert taken == []
