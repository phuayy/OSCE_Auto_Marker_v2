"""The accelerator lease (F10).

``JOB_WORKER_CONCURRENCY`` bounds jobs; nothing bounded the GPU. Two jobs each
loading WhisperX into one card produced a CUDA OOM that the pipeline classified
as a permanent resource failure — a session lost to contention the queue
itself created. These tests pin the lease that makes GPU steps take turns, and
that it is taken where every engine and detector runs rather than inside each.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.core.config import Settings
from app.core.resources import ResourceLease
from app.pipeline.media import MediaPipeline
from app.pipeline.transcription.base import (
    EngineCapabilities,
    EngineDescriptor,
    TranscriptionEngine,
    TranscriptionRequest,
    TranscriptionResult,
)
from app.services.transcription_router import TranscriptionRouter
from tests.fixtures.events import RecordingEvents as _Events


def test_bounded_lease_serialises_holders_and_reports_waits() -> None:
    lease = ResourceLease(1, "gpu")
    peak = {"in_use": 0}
    order: list[str] = []

    async def worker(name: str) -> None:
        async with lease.hold(name):
            order.append(f"{name}:start")
            peak["in_use"] = max(peak["in_use"], lease.in_use)
            await asyncio.sleep(0.01)
            order.append(f"{name}:end")

    async def scenario() -> None:
        await asyncio.gather(worker("a"), worker("b"), worker("c"))

    asyncio.run(scenario())

    assert peak["in_use"] == 1, "two holders overlapped on a one-slot lease"
    # Every start is preceded by the previous holder's end.
    assert order == ["a:start", "a:end", "b:start", "b:end", "c:start", "c:end"]
    assert lease.in_use == 0 and lease.waiting == 0
    assert lease.stats() == {"name": "gpu", "slots": 1, "inUse": 0, "waiting": 0}


def test_unbounded_lease_lets_everyone_through_at_once() -> None:
    lease = ResourceLease.unbounded("gpu")
    concurrent = {"peak": 0, "now": 0}

    async def worker() -> None:
        async with lease.hold("x"):
            concurrent["now"] += 1
            concurrent["peak"] = max(concurrent["peak"], concurrent["now"])
            await asyncio.sleep(0.01)
            concurrent["now"] -= 1

    async def scenario() -> None:
        await asyncio.gather(worker(), worker(), worker())

    asyncio.run(scenario())

    assert not lease.bounded
    assert concurrent["peak"] == 3
    assert lease.stats()["slots"] is None


def test_gpu_slots_zero_or_negative_means_unbounded() -> None:
    assert not ResourceLease(0, "gpu").bounded
    assert not ResourceLease(-3, "gpu").bounded
    assert ResourceLease(2, "gpu").slots == 2


def test_media_pipeline_has_a_lease_even_when_constructed_without_one(tmp_path: Path) -> None:
    """Test doubles subclass MediaPipeline without calling __init__; production
    passes the shared lease. Both must be able to ``hold``."""

    class Double(MediaPipeline):
        def __init__(self) -> None:  # noqa: D401 - deliberately skips the parent
            pass

    assert isinstance(Double().gpu, ResourceLease)
    shared = ResourceLease(1, "gpu")
    settings = Settings(root_dir=tmp_path, backend_root=tmp_path, ffmpeg_bin="f", ffprobe_bin="f", scorer_python_bin="p")
    media = MediaPipeline(settings, runner=None, events=None, auth=None, gpu=shared)  # type: ignore[arg-type]
    assert media.gpu is shared


class _LeaseWatchingEngine(TranscriptionEngine):
    """Records how many slots of the router's lease are held while it runs."""

    descriptor = EngineDescriptor(
        id="watch",
        label="Watch",
        vendor="test",
        description="",
        capabilities=EngineCapabilities(diarization=True),
    )

    def __init__(self, lease: ResourceLease) -> None:
        self.lease = lease
        self.in_use_during_run: int | None = None

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        self.in_use_during_run = self.lease.in_use
        await asyncio.sleep(0)
        return TranscriptionResult(
            engine_id="watch",
            model="m",
            diarized=True,
            json_path=request.output_dir / "x.json",
            srt_path=None,
            vtt_path=None,
        )


def test_router_holds_the_lease_around_whichever_engine_runs(tmp_path: Path) -> None:
    settings = Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        ffmpeg_bin="f",
        ffprobe_bin="f",
        scorer_python_bin="p",
        transcription_engine="watch",
    )
    lease = ResourceLease(1, "gpu")
    engine = _LeaseWatchingEngine(lease)

    router = TranscriptionRouter.__new__(TranscriptionRouter)
    router.settings = settings
    router.events = _Events()
    router.app_settings = None
    router.engines = {"watch": engine}
    router.gpu = lease

    result = asyncio.run(
        router.transcribe(
            {"id": "s1"},
            {"absolutePath": str(tmp_path / "a.mp3"), "fileName": "a.mp3"},
        )
    )

    assert result.engine_id == "watch"
    assert engine.in_use_during_run == 1, "the engine ran without the router holding the GPU lease"
    assert lease.in_use == 0, "the lease was not released after the run"
