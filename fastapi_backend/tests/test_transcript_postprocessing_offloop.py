"""Transcript post-processing must not freeze the event loop.

Corpus correction is pure CPU (difflib + Double Metaphone per window per term):
tens of seconds for a realistic corpus. Run inline in the pipeline's coroutine
it stalls every HTTP request, the change feed and the job heartbeats of the
process. These tests pin three things: the optimised matcher still produces
byte-identical output (golden captured from the pre-optimisation code), the
loop keeps ticking while a large corpus is corrected, and the edge cases of the
step keep their behaviour across the thread hop.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from app.pipeline import transcript_correction
from app.pipeline.transcript_correction import CorrectionPolicy, correct_segments
from app.services import pipeline_service
from app.services.pipeline_service import PipelineService

from tests.golden_correction_fixture import build_fixture
from tests.test_pipeline_service import FakeEvents, FakeMedia, build_settings

GOLDEN = Path(__file__).parent / "data" / "transcript_correction_golden.json"


def build_service(tmp_path: Path) -> PipelineService:
    return PipelineService(
        sessions=None, events=FakeEvents(), media=FakeMedia(build_settings(tmp_path)), scoring=None
    )


async def max_loop_gap(work: Any) -> tuple[float, float]:
    """Run ``work()`` while a 10 ms ticker runs; return (largest tick gap, work duration)."""
    gaps: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.05)
    started = time.perf_counter()
    await work()
    elapsed = time.perf_counter() - started
    stop.set()
    await task
    return max(gaps), elapsed


def test_correction_output_is_identical_to_the_golden_capture() -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    segments, terms = build_fixture(40, 40)

    corrections = correct_segments(segments, terms, CorrectionPolicy())

    assert corrections == golden["corrections"]
    assert [segment["text"] for segment in segments] == golden["texts"]


def test_segments_without_text_and_unicode_are_handled() -> None:
    segments = [{"id": 1, "text": None}, {"id": 2}, {"id": 3, "text": "  "}, {"id": 4, "text": "Café, the nasal blog naïve."}]
    corrections = correct_segments(segments, ["nasal block", "paracetamol"])

    assert segments[3]["text"] == "Café, the nasal block naïve."
    assert [c["segmentId"] for c in corrections] == [4]
    assert correct_segments([], ["paracetamol"]) == []


def test_loop_stays_responsive_while_a_large_corpus_is_corrected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = build_service(tmp_path)
    segments, terms = build_fixture(60, 40)
    real = transcript_correction.correct_segments

    def at_least_600ms(*args: Any, **kwargs: Any) -> Any:
        # The matcher is fast enough now that the real work alone may not span
        # the ticker's resolution; pad it so a blocked loop is always visible.
        result = real(*args, **kwargs)
        time.sleep(0.6)
        return result

    monkeypatch.setattr(pipeline_service, "correct_segments", at_least_600ms)
    session = {"id": "s1", "corpus": {"name": "big", "terms": terms}}
    transcript = {"segments": segments}

    gap, elapsed = asyncio.run(
        max_loop_gap(lambda: service._apply_corpus_corrections(session, transcript, {}))
    )

    assert transcript["corrections"], "fixture should exercise real corrections"
    assert gap < 0.25, f"event loop stalled for {gap:.2f}s during a {elapsed:.2f}s correction"


def test_loop_stays_responsive_while_screening(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = build_service(tmp_path)

    def slow_screen(segments: list[dict[str, Any]], **_: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        time.sleep(0.6)
        return segments, []

    monkeypatch.setattr(pipeline_service, "screen_segments", slow_screen)
    transcript = {"segments": [{"id": 1, "text": "hello there"}]}

    gap, _ = asyncio.run(max_loop_gap(lambda: service._screen_hallucinations({"id": "s1"}, transcript)))

    assert gap < 0.25
    assert transcript["hallucinations"] == []


def test_empty_corpus_returns_early_without_a_thread_hop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = build_service(tmp_path)
    hops: list[Any] = []

    async def spy(func: Any, *args: Any, **kwargs: Any) -> Any:
        hops.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(pipeline_service.asyncio, "to_thread", spy)
    transcript = {"segments": [{"id": 1, "text": "nasal blog"}]}

    asyncio.run(service._apply_corpus_corrections({"id": "s1", "corpus": {"name": "c", "terms": []}}, transcript, {}))

    assert hops == []
    assert transcript["corrections"] == []
    assert transcript["segments"][0]["text"] == "nasal blog"


def test_transcript_with_no_segments_is_a_no_op(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    transcript: dict[str, Any] = {"segments": []}

    asyncio.run(service._apply_corpus_corrections({"id": "s1", "corpus": {"name": "c", "terms": ["paracetamol"]}}, transcript, {}))
    asyncio.run(service._screen_hallucinations({"id": "s1"}, transcript))

    assert transcript["corrections"] == []
    assert transcript["hallucinations"] == []


def test_screening_disabled_by_settings_skips_the_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = PipelineService(
        sessions=None,
        events=FakeEvents(),
        media=FakeMedia(build_settings(tmp_path, transcript_hallucination_filter=False)),
        scoring=None,
    )

    def boom(*_: Any, **__: Any) -> Any:
        raise AssertionError("screening must not run when disabled")

    monkeypatch.setattr(pipeline_service, "screen_segments", boom)
    transcript = {"segments": [{"id": 1, "text": "hello"}]}

    asyncio.run(service._screen_hallucinations({"id": "s1"}, transcript))

    assert transcript["hallucinations"] == []


def test_exception_in_the_threaded_correction_is_recorded_the_same_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = build_service(tmp_path)

    def explode(*_: Any, **__: Any) -> Any:
        raise RuntimeError("matcher blew up")

    monkeypatch.setattr(pipeline_service, "correct_segments", explode)
    transcript = {"segments": [{"id": 1, "text": "nasal blog"}]}

    asyncio.run(service._apply_corpus_corrections({"id": "s1", "corpus": {"name": "c", "terms": ["nasal block"]}}, transcript, {}))

    assert transcript["corrections"] == []
    assert transcript["segments"][0]["text"] == "nasal blog"


def test_threaded_correction_still_patches_subtitle_files(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    srt = tmp_path / "a.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nI have a nasal blog\n", encoding="utf-8")
    transcript = {"segments": [{"id": 1, "text": "I have a nasal blog"}]}

    asyncio.run(
        service._apply_corpus_corrections(
            {"id": "s1", "corpus": {"name": "c", "terms": ["nasal block"]}}, transcript, {"srtAbsolutePath": str(srt)}
        )
    )

    assert transcript["segments"][0]["text"] == "I have a nasal block"
    assert "nasal block" in srt.read_text(encoding="utf-8")


def test_module_exports_are_the_ones_the_pipeline_patches() -> None:
    assert pipeline_service.correct_segments is transcript_correction.correct_segments
