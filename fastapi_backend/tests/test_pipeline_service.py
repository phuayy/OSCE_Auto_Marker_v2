from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.core.exceptions import AppError
from app.pipeline.media import MediaPipeline
from app.services.pipeline_service import PipelineService
from tests.fixtures.scoring_doubles import ContentMarkingSeam
from tests.fixtures.session_store import SessionUpdateMixin
from tests.fixtures.events import RecordingEvents as FakeEvents


class FakeSessions(SessionUpdateMixin):
    def __init__(self, initial_session: dict[str, Any] | None = None) -> None:
        self.current = copy.deepcopy(initial_session) if initial_session is not None else None
        self.writes: list[dict[str, Any]] = []

    async def read(self, session_id: str) -> dict[str, Any]:
        if self.current is None or str(self.current.get("id")) != str(session_id):
            raise FileNotFoundError(f"Session not found: {session_id}")
        return copy.deepcopy(self.current)

    async def write(self, session: dict[str, Any]) -> None:
        self.current = copy.deepcopy(session)
        self.writes.append(copy.deepcopy(session))

    def public_session(self, session: dict[str, Any]) -> dict[str, Any]:
        return session


class FakeMedia:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def ensure_session_subtitle_track(self, _session: dict[str, Any]) -> bool:
        return False


def build_settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        **overrides,
    )


# A non-empty transcript. The pipeline rejects a transcript with no speech
# segments as a failed transcription (see test_empty_transcript_guard.py), so
# fixtures exercising the cached/resume paths must carry real segment text.
TRANSCRIPT_JSON = (
    '{"schema": "whisperx-segments-v1", "segmentCount": 1, '
    '"segments": [{"id": 1, "speaker": "SPEAKER_00", "start": 0.0, "end": 2.0, "text": "Good morning."}]}'
)


def build_session(tmp_path: Path) -> dict[str, Any]:
    return {
        "id": "session-1",
        "files": {
            "video": {"absolutePath": str(tmp_path / "video.mp4")},
            "caseStudy": {"absolutePath": str(tmp_path / "case.pdf")},
        },
        "outputs": {
            "audio": {"absolutePath": str(tmp_path / "audio.mp3")},
            "transcript": {"absolutePath": str(tmp_path / "transcript.json")},
        },
    }


def test_parallel_scoring_overlaps_content_with_audio_branch(tmp_path) -> None:
    settings = build_settings(tmp_path, parallel_scoring=True)
    events = FakeEvents()
    session = build_session(tmp_path)
    sessions = FakeSessions(session)

    class FakeScoring(ContentMarkingSeam):
        def __init__(self) -> None:
            self.audio_started = asyncio.Event()
            self.content_started = asyncio.Event()

        async def run_audio_professionalism(self, _session: dict[str, Any]) -> dict[str, Any]:
            self.audio_started.set()
            await asyncio.wait_for(self.content_started.wait(), timeout=1)
            return {"absolutePath": "audio-prof.json", "payload": {"schema": "audio-professionalism-v1"}}

        async def run_communication_scoring(
            self,
            _session: dict[str, Any],
            audio_professionalism: dict[str, Any] | None,
        ) -> dict[str, Any]:
            assert audio_professionalism and audio_professionalism["absolutePath"] == "audio-prof.json"
            return {"absolutePath": "communication.json", "payload": {"schema": "communication-scoring-v2"}}

        async def run_content_scoring(self, _session: dict[str, Any]) -> dict[str, Any]:
            await asyncio.wait_for(self.audio_started.wait(), timeout=1)
            self.content_started.set()
            return {"absolutePath": "scores.json", "payload": {"schema": "content-scoring-v1"}}

    service = PipelineService(
        sessions=sessions,
        events=events,
        media=FakeMedia(settings),
        scoring=FakeScoring(),
    )

    result = asyncio.run(service._run_cached_scoring_branches(session))

    assert result["audioProfessionalism"]["schema"] == "audio-professionalism-v1"
    assert result["communicationScores"]["schema"] == "communication-scoring-v2"
    assert result["scores"]["schema"] == "content-scoring-v1"
    assert sessions.writes[-1]["outputs"]["scores"]["absolutePath"] == "scores.json"


def test_parallel_scoring_persists_successful_branch_before_raising(tmp_path) -> None:
    settings = build_settings(tmp_path, parallel_scoring=True)
    session = build_session(tmp_path)
    sessions = FakeSessions(session)

    class PartiallyFailingScoring(ContentMarkingSeam):
        async def run_audio_professionalism(self, _session: dict[str, Any]) -> dict[str, Any]:
            return {"absolutePath": "audio-prof.json", "payload": {"schema": "audio-professionalism-v1"}}

        async def run_communication_scoring(
            self,
            _session: dict[str, Any],
            _audio_professionalism: dict[str, Any] | None,
        ) -> dict[str, Any]:
            raise RuntimeError("communication provider failed")

        async def run_content_scoring(self, _session: dict[str, Any]) -> dict[str, Any]:
            return {"absolutePath": "scores.json", "payload": {"schema": "content-scoring-v1"}}

    service = PipelineService(
        sessions=sessions,
        events=FakeEvents(),
        media=FakeMedia(settings),
        scoring=PartiallyFailingScoring(),
    )

    with pytest.raises(RuntimeError, match="communication provider failed"):
        asyncio.run(service._run_cached_scoring_branches(session))

    persisted_outputs = sessions.writes[-1]["outputs"]
    assert persisted_outputs["audioProfessionalism"]["absolutePath"] == "audio-prof.json"
    assert persisted_outputs["scores"]["absolutePath"] == "scores.json"
    assert persisted_outputs["communicationScores"] is None


def test_process_session_rejects_processing_session_without_worker_override(tmp_path) -> None:
    transcript_path = tmp_path / "transcript.json"
    transcript_path.write_text(TRANSCRIPT_JSON, encoding="utf-8")
    session = build_session(tmp_path)
    session["status"] = "processing"
    session["outputs"]["transcript"]["absolutePath"] = str(transcript_path)
    settings = build_settings(
        tmp_path,
        enable_audio_professionalism=False,
        enable_communication_scoring=False,
        enable_scoring=False,
    )
    service = PipelineService(
        sessions=FakeSessions(session),
        events=FakeEvents(),
        media=FakeMedia(settings),
        scoring=object(),
    )

    with pytest.raises(AppError, match="already being processed"):
        asyncio.run(service.process_session_by_id("session-1"))


def test_process_session_allows_claimed_worker_to_resume_processing_session(tmp_path) -> None:
    transcript_path = tmp_path / "transcript.json"
    transcript_path.write_text(TRANSCRIPT_JSON, encoding="utf-8")
    session = build_session(tmp_path)
    session["status"] = "processing"
    session["outputs"]["transcript"]["absolutePath"] = str(transcript_path)
    settings = build_settings(
        tmp_path,
        enable_audio_professionalism=False,
        enable_communication_scoring=False,
        enable_scoring=False,
    )
    sessions = FakeSessions(session)
    service = PipelineService(
        sessions=sessions,
        events=FakeEvents(),
        media=FakeMedia(settings),
        scoring=object(),
    )

    result = asyncio.run(service.process_session_by_id("session-1", allow_processing=True))

    assert result["transcript"]["segments"][0]["text"] == "Good morning."
    assert sessions.writes[-1]["status"] == "completed"


def test_cached_parallel_scoring_overlaps_content_with_audio_branch(tmp_path) -> None:
    settings = build_settings(tmp_path, parallel_scoring=True)
    session = build_session(tmp_path)
    sessions = FakeSessions(session)

    class FakeScoring(ContentMarkingSeam):
        def __init__(self) -> None:
            self.audio_started = asyncio.Event()
            self.content_started = asyncio.Event()

        @staticmethod
        def should_refresh_audio_professionalism_payload(_payload: Any) -> bool:
            return True

        @staticmethod
        def should_refresh_communication_payload(_payload: Any) -> bool:
            return True

        @staticmethod
        def should_refresh_score_payload(_payload: Any) -> bool:
            return True

        async def run_audio_professionalism(self, _session: dict[str, Any]) -> dict[str, Any]:
            self.audio_started.set()
            await asyncio.wait_for(self.content_started.wait(), timeout=1)
            return {"absolutePath": "audio-prof.json", "payload": {"schema": "audio-professionalism-v1"}}

        async def run_communication_scoring(
            self,
            _session: dict[str, Any],
            audio_professionalism: dict[str, Any] | None,
        ) -> dict[str, Any]:
            assert audio_professionalism and audio_professionalism["absolutePath"] == "audio-prof.json"
            return {"absolutePath": "communication.json", "payload": {"schema": "communication-scoring-v2"}}

        async def run_content_scoring(self, _session: dict[str, Any]) -> dict[str, Any]:
            await asyncio.wait_for(self.audio_started.wait(), timeout=1)
            self.content_started.set()
            return {"absolutePath": "scores.json", "payload": {"schema": "content-scoring-v1"}}

    service = PipelineService(
        sessions=sessions,
        events=FakeEvents(),
        media=FakeMedia(settings),
        scoring=FakeScoring(),
    )

    result = asyncio.run(service._run_cached_scoring_branches(session))

    assert result["audioProfessionalism"]["schema"] == "audio-professionalism-v1"
    assert result["communicationScores"]["schema"] == "communication-scoring-v2"
    assert result["scores"]["schema"] == "content-scoring-v1"


def test_cached_parallel_scoring_persists_successful_branch_before_raising(tmp_path) -> None:
    settings = build_settings(tmp_path, parallel_scoring=True)
    session = build_session(tmp_path)
    sessions = FakeSessions(session)

    class PartiallyFailingScoring(ContentMarkingSeam):
        @staticmethod
        def should_refresh_audio_professionalism_payload(_payload: Any) -> bool:
            return True

        @staticmethod
        def should_refresh_communication_payload(_payload: Any) -> bool:
            return True

        @staticmethod
        def should_refresh_score_payload(_payload: Any) -> bool:
            return True

        async def run_audio_professionalism(self, _session: dict[str, Any]) -> dict[str, Any]:
            return {"absolutePath": "audio-prof.json", "payload": {"schema": "audio-professionalism-v1"}}

        async def run_communication_scoring(
            self,
            _session: dict[str, Any],
            _audio_professionalism: dict[str, Any] | None,
        ) -> dict[str, Any]:
            raise RuntimeError("communication provider failed")

        async def run_content_scoring(self, _session: dict[str, Any]) -> dict[str, Any]:
            return {"absolutePath": "scores.json", "payload": {"schema": "content-scoring-v1"}}

    service = PipelineService(
        sessions=sessions,
        events=FakeEvents(),
        media=FakeMedia(settings),
        scoring=PartiallyFailingScoring(),
    )

    with pytest.raises(RuntimeError, match="communication provider failed"):
        asyncio.run(service._run_cached_scoring_branches(session))

    persisted_outputs = sessions.writes[-1]["outputs"]
    assert persisted_outputs["audioProfessionalism"]["absolutePath"] == "audio-prof.json"
    assert persisted_outputs["scores"]["absolutePath"] == "scores.json"


def test_ensure_audio_output_reuses_deterministic_mp3_after_crash(tmp_path) -> None:
    settings = build_settings(tmp_path)
    settings.paths.ensure_layout()
    session = build_session(tmp_path)
    session["outputs"].pop("audio")
    audio_path = settings.paths.output_audio_dir / "session-1.mp3"
    audio_path.write_bytes(b"mp3")

    class NoExtractMedia(FakeMedia):
        async def extract_audio_to_mp3(self, _session: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("ffmpeg should not be rerun when deterministic MP3 exists")

    sessions = FakeSessions(session)
    service = PipelineService(
        sessions=sessions,
        events=FakeEvents(),
        media=NoExtractMedia(settings),
        scoring=object(),
    )

    audio_info = asyncio.run(service._ensure_audio_output(session))

    assert audio_info["absolutePath"] == str(audio_path)
    assert sessions.writes[-1]["outputs"]["audio"]["absolutePath"] == str(audio_path)
    step = sessions.writes[-1]["pipeline"]["steps"]["audio_extraction"]
    assert step["status"] == "completed"
    assert step["metadata"]["reusedExistingArtifact"] is True


def test_cached_scoring_discovers_existing_content_output_without_session_metadata(tmp_path) -> None:
    settings = build_settings(tmp_path)
    settings.paths.ensure_layout()
    session = build_session(tmp_path)
    session["outputs"].pop("scores", None)
    payload = {"ok": True}
    score_path = settings.paths.output_scores_dir / "session-1.json"
    score_path.write_text('{"ok": true}\n', encoding="utf-8")

    class NoRunScoring(ContentMarkingSeam):
        @staticmethod
        def should_refresh_score_payload(_payload: Any) -> bool:
            return False

        async def run_content_scoring(self, _session: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("content scoring should not be rerun when deterministic JSON exists")

    sessions = FakeSessions(session)
    service = PipelineService(
        sessions=sessions,
        events=FakeEvents(),
        media=FakeMedia(settings),
        scoring=NoRunScoring(),
    )

    result = asyncio.run(service._refresh_or_load_content_scores(session))

    assert result["payload"] == payload
    assert sessions.writes[-1]["outputs"]["scores"]["absolutePath"] == str(score_path)
    step = sessions.writes[-1]["pipeline"]["steps"]["content_scoring"]
    assert step["status"] == "completed"
    assert step["metadata"]["reusedExistingArtifact"] is True


def test_media_reuses_existing_whisperx_json_before_cli(tmp_path) -> None:
    settings = build_settings(tmp_path)
    settings.paths.ensure_layout()
    events = FakeEvents()
    output_dir = settings.paths.output_whisperx_dir / "session-1"
    output_dir.mkdir(parents=True, exist_ok=True)
    whisperx_json = output_dir / "session-1.json"
    whisperx_json.write_text(TRANSCRIPT_JSON + "\n", encoding="utf-8")

    class NoRunRunner:
        async def run(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("WhisperX CLI should not run when JSON artifact exists")

    class Auth:
        runtime = type("Runtime", (), {"whisperx_hf_token": "token"})()

    from app.core.resources import ResourceLease

    media = MediaPipeline(settings, NoRunRunner(), events, Auth(), gpu=ResourceLease.unbounded())

    result = asyncio.run(
        media.run_whisperx_transcription(
            {"id": "session-1"},
            {"fileName": "session-1.mp3", "absolutePath": str(settings.paths.output_audio_dir / "session-1.mp3")},
        )
    )

    assert result["jsonAbsolutePath"] == whisperx_json
    assert any("Reusing existing WhisperX JSON" in item[2]["message"] for item in events.items)
