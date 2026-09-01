"""A transcription with no speech must fail the run, never be scored.

The transcript is the only input the three scorers read, so an empty one does
not yield an empty result — it yields a full, confident-looking assessment of
nothing. These tests pin the three gates that make that impossible:

* ``MediaPipeline.count_usable_segments`` — the shared "is this usable" rule.
* the WhisperX artifact cache — an empty artifact is never reused, and a run
  that produces one fails with a distinguishable error.
* ``PipelineService`` — both the fresh and the cached path refuse to score an
  empty transcript.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings
from app.core.exceptions import EmptyTranscriptError
from app.core.process import CommandResult
from app.pipeline.media import MediaPipeline
from app.services.pipeline_service import PipelineService

from tests.test_pipeline_service import FakeEvents, FakeMedia, FakeSessions, build_session, build_settings


SPOKEN_SEGMENT = {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00", "text": "Good morning."}


class CapturingEvents:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict[str, Any]]] = []

    async def publish(self, session_id: str, event_name: str, payload: dict[str, Any]) -> None:
        self.items.append((session_id, event_name, payload))


class WhisperxRunner:
    """Stands in for the WhisperX CLI, writing a caller-chosen JSON artifact."""

    def __init__(self, settings: Settings, payload: dict[str, Any] | None) -> None:
        self.settings = settings
        self.payload = payload
        self.runs = 0

    async def run(self, command: str, args: list[str], _label: str, **_kwargs: Any) -> CommandResult:
        if command == self.settings.ffmpeg_bin:
            Path(args[-1]).write_bytes(b"fake-audio")
            return CommandResult(stdout="", stderr="")
        self.runs += 1
        if self.payload is not None:
            output_dir = Path(args[args.index("--output_dir") + 1])
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / f"{Path(args[0]).stem}.json").write_text(json.dumps(self.payload), encoding="utf-8")
        return CommandResult(stdout="", stderr="")


def make_media(tmp_path: Path, payload: dict[str, Any] | None) -> tuple[MediaPipeline, WhisperxRunner]:
    settings = Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        whisperx_device="cpu",
    )
    settings.paths.ensure_layout()
    runner = WhisperxRunner(settings, payload)
    auth = SimpleNamespace(runtime=SimpleNamespace(whisperx_hf_token="hf-token"))
    return MediaPipeline(settings, runner, CapturingEvents(), auth), runner


def run_transcription(media: MediaPipeline) -> dict[str, Any]:
    mp3_path = media.settings.paths.output_audio_dir / "session-1.mp3"
    mp3_path.write_bytes(b"fake-mp3")
    return asyncio.run(
        media.run_whisperx_transcription(
            {"id": "session-1"},
            {"fileName": mp3_path.name, "absolutePath": str(mp3_path)},
        )
    )


# --- the shared usability rule ---------------------------------------------


def test_count_usable_segments_ignores_blank_and_malformed_entries() -> None:
    assert MediaPipeline.count_usable_segments({"segments": [SPOKEN_SEGMENT]}) == 1
    assert MediaPipeline.count_usable_segments({"segments": []}) == 0
    assert MediaPipeline.count_usable_segments({"segments": [{"text": "   "}, {"text": ""}]}) == 0
    assert MediaPipeline.count_usable_segments({"segments": ["not-a-dict", None]}) == 0
    assert MediaPipeline.count_usable_segments({"segments": "not-a-list"}) == 0
    assert MediaPipeline.count_usable_segments({}) == 0
    assert MediaPipeline.count_usable_segments(None) == 0


def test_count_usable_segments_agrees_with_normalization(tmp_path: Path) -> None:
    """The cache rule and the normalizer must not disagree: anything counted as
    usable has to survive normalization, or an "acceptable" artifact could still
    normalize down to nothing."""
    media, _ = make_media(tmp_path, payload=None)
    raw = {"segments": [SPOKEN_SEGMENT, {"start": 3.0, "end": 4.0, "text": "  "}, "junk"]}

    assert MediaPipeline.count_usable_segments(raw) == 1
    assert media.normalize_whisperx_transcript(raw)["segmentCount"] == 1


# --- WhisperX artifact cache ------------------------------------------------


def test_empty_whisperx_artifact_is_not_reused_as_cache(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path, payload={"segments": [SPOKEN_SEGMENT]})
    stale_dir = media.settings.paths.output_whisperx_dir / "session-1"
    stale_dir.mkdir(parents=True, exist_ok=True)
    (stale_dir / "session-1.json").write_text(json.dumps({"segments": []}), encoding="utf-8")

    outputs = run_transcription(media)

    # The empty artifact was rejected, so the CLI ran and overwrote it.
    assert runner.runs == 1
    payload = json.loads(Path(str(outputs["jsonAbsolutePath"])).read_text(encoding="utf-8"))
    assert MediaPipeline.count_usable_segments(payload) == 1


def test_whisperx_run_producing_no_speech_raises_empty_transcript_error(tmp_path: Path) -> None:
    media, _ = make_media(tmp_path, payload={"segments": []})

    with pytest.raises(EmptyTranscriptError, match="no usable speech segments"):
        run_transcription(media)


def test_whisperx_run_producing_no_file_keeps_its_own_error(tmp_path: Path) -> None:
    """A missing artifact and an empty one are different faults with different
    fixes, so they must not collapse into one message."""
    media, _ = make_media(tmp_path, payload=None)

    with pytest.raises(RuntimeError, match="no JSON output file") as excinfo:
        run_transcription(media)
    assert not isinstance(excinfo.value, EmptyTranscriptError)


# --- pipeline gates ---------------------------------------------------------


def build_pipeline(tmp_path: Path, sessions: FakeSessions) -> PipelineService:
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


def test_cached_path_refuses_to_score_an_empty_transcript(tmp_path: Path) -> None:
    transcript_path = tmp_path / "transcript.json"
    transcript_path.write_text('{"schema": "whisperx-segments-v1", "segments": []}', encoding="utf-8")
    session = build_session(tmp_path)
    session["outputs"]["transcript"]["absolutePath"] = str(transcript_path)
    sessions = FakeSessions(session)
    service = build_pipeline(tmp_path, sessions)

    with pytest.raises(EmptyTranscriptError, match="nothing to score"):
        asyncio.run(service.process_session_by_id("session-1"))


def test_empty_transcript_error_is_a_permanent_4xx() -> None:
    """Surfaced as a client error so the API returns something actionable and
    the local job runner treats it as permanent rather than retrying it."""
    error = EmptyTranscriptError("no speech")

    assert error.status_code == 422
    assert error.message == "no speech"
