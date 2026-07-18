from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.core.json_utils import read_json_file
from app.pipeline.llm_preprocess import diff_replacements, merge_corrected_segments
from app.services.pipeline_service import PipelineService


def build_transcript() -> dict[str, Any]:
    return {
        "schema": "whisperx-segments-v1",
        "segments": [
            {"id": 1, "speaker": "SPEAKER_00", "start": 0.0, "end": 2.0, "text": "I have a block nurse today."},
            {"id": 2, "speaker": "SPEAKER_01", "start": 2.0, "end": 4.0, "text": "Take parasympamol twice daily."},
            {"id": 3, "speaker": "SPEAKER_00", "start": 4.0, "end": 5.0, "text": "Thank you."},
        ],
    }


def test_merge_applies_by_id_and_logs_changes() -> None:
    transcript = build_transcript()
    changes = merge_corrected_segments(
        transcript,
        [
            {"id": 1, "text": "I have a blocked nose today."},
            {"id": 2, "text": "Take paracetamol twice daily."},
            {"id": 3, "text": "Thank you."},
        ],
    )
    assert transcript["segments"][0]["text"] == "I have a blocked nose today."
    assert transcript["segments"][1]["text"] == "Take paracetamol twice daily."
    assert [change["segmentId"] for change in changes] == [1, 2]
    # Timestamps and speakers are untouched by design.
    assert transcript["segments"][0]["start"] == 0.0
    assert transcript["segments"][0]["speaker"] == "SPEAKER_00"


def test_merge_missing_id_falls_back_and_invented_id_is_ignored() -> None:
    transcript = build_transcript()
    changes = merge_corrected_segments(
        transcript,
        [{"id": 2, "text": "Take paracetamol twice daily."}, {"id": 99, "text": "hallucinated"}],
    )
    assert transcript["segments"][0]["text"] == "I have a block nurse today."
    assert len(changes) == 1 and changes[0]["segmentId"] == 2


def test_merge_empty_reply_raises_for_transcript_with_text() -> None:
    with pytest.raises(ValueError):
        merge_corrected_segments(build_transcript(), [])


def test_merge_empty_reply_is_fine_for_empty_transcript() -> None:
    assert merge_corrected_segments({"segments": [{"id": 1, "text": "  "}]}, []) == []


def test_diff_replacements_word_level() -> None:
    replacements = diff_replacements("Take parasympamol twice daily.", "Take paracetamol twice daily.")
    assert replacements == [{"original": "parasympamol", "corrected": "paracetamol"}]
    assert diff_replacements("same text", "same text") == []


class _FakeSessions:
    def __init__(self) -> None:
        self.session: dict[str, Any] | None = None
        self.writes = 0

    async def write(self, session: dict[str, Any]) -> None:
        self.writes += 1
        self.session = session


class _FakeEvents:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, dict | None]] = []

    async def publish(self, session_id: str, name: str, payload: dict | None = None) -> None:
        self.published.append((session_id, name, payload))


class _FakeAppSettings:
    def __init__(self, enabled: bool) -> None:
        self._enabled = enabled

    async def llm_preprocess_enabled(self) -> bool:
        return self._enabled


class _FakePreprocessor:
    def __init__(self, payload: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls = 0

    async def run(self, _session: dict[str, Any], _transcript_path: Path) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.payload or {}


def build_service(preprocessor: Any, enabled: bool | None) -> PipelineService:
    app_settings = _FakeAppSettings(enabled) if enabled is not None else None
    return PipelineService(
        _FakeSessions(),
        events=_FakeEvents(),
        media=None,
        scoring=None,
        preprocessor=preprocessor,
        app_settings=app_settings,
    )


def write_transcript(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    transcript = build_transcript()
    transcript_path = tmp_path / "transcript.json"
    transcript_path.write_text(json.dumps(transcript), encoding="utf-8")
    return transcript, transcript_path


def test_step_completes_rewrites_transcript_and_patches_subtitles(tmp_path: Path) -> None:
    transcript, transcript_path = write_transcript(tmp_path)
    srt_path = tmp_path / "audio.srt"
    srt_path.write_text("1\n00:00:02,000 --> 00:00:04,000\nTake parasympamol twice daily.\n", encoding="utf-8")

    preprocessor = _FakePreprocessor(
        payload={
            "schema": "llm-preprocess-v1",
            "model": "nvidia/nemotron-test",
            "segments": [
                {"id": 1, "text": "I have a blocked nose today."},
                {"id": 2, "text": "Take paracetamol twice daily."},
                {"id": 3, "text": "Thank you."},
            ],
        }
    )
    service = build_service(preprocessor, enabled=True)
    session = {
        "id": "sess-1",
        "pipeline": {},
        "outputs": {"transcript": {"absolutePath": str(transcript_path), "sizeBytes": 1}},
    }

    asyncio.run(
        service._run_llm_preprocess(session, transcript, {"srtAbsolutePath": str(srt_path)}, transcript_path)
    )

    step = session["pipeline"]["steps"]["llm_preprocess"]
    assert step["status"] == "completed"
    assert step["metadata"]["changedSegments"] == 2
    stored = read_json_file(transcript_path)
    assert stored["segments"][1]["text"] == "Take paracetamol twice daily."
    assert stored["llmPreprocess"]["applied"] is True
    assert len(stored["llmPreprocess"]["changes"]) == 2
    assert "paracetamol" in srt_path.read_text(encoding="utf-8")
    assert session["outputs"]["transcript"]["sizeBytes"] == transcript_path.stat().st_size


def test_step_skipped_when_toggle_off(tmp_path: Path) -> None:
    transcript, transcript_path = write_transcript(tmp_path)
    original_bytes = transcript_path.read_bytes()
    preprocessor = _FakePreprocessor()
    service = build_service(preprocessor, enabled=False)
    session = {"id": "sess-1", "pipeline": {}, "outputs": {}}

    asyncio.run(service._run_llm_preprocess(session, transcript, {}, transcript_path))

    assert session["pipeline"]["steps"]["llm_preprocess"]["status"] == "skipped"
    assert preprocessor.calls == 0
    assert transcript_path.read_bytes() == original_bytes


def test_step_skipped_when_not_wired() -> None:
    # Constructor defaults (no preprocessor/app_settings) never break the run.
    service = PipelineService(_FakeSessions(), events=_FakeEvents(), media=None, scoring=None)
    session = {"id": "sess-1", "pipeline": {}, "outputs": {}}

    asyncio.run(service._run_llm_preprocess(session, {"segments": []}, {}, Path("missing.json")))

    assert session["pipeline"]["steps"]["llm_preprocess"]["status"] == "skipped"


def test_step_failure_is_recorded_but_does_not_raise(tmp_path: Path) -> None:
    transcript, transcript_path = write_transcript(tmp_path)
    original_bytes = transcript_path.read_bytes()
    service = build_service(_FakePreprocessor(error=RuntimeError("model exploded")), enabled=True)
    session = {"id": "sess-1", "pipeline": {}, "outputs": {}}

    asyncio.run(service._run_llm_preprocess(session, transcript, {}, transcript_path))

    step = session["pipeline"]["steps"]["llm_preprocess"]
    assert step["status"] == "failed"
    assert "model exploded" in step["error"]
    # Scoring proceeds with the uncorrected transcript artifact.
    assert transcript_path.read_bytes() == original_bytes
