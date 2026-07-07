from __future__ import annotations

import asyncio

from app.core.config import Settings
from app.services.event_service import EventService


def _service(**overrides) -> EventService:
    settings = Settings(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", scorer_python_bin="python", **overrides)
    return EventService(settings)


def test_offer_drops_oldest_when_queue_full() -> None:
    queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    EventService._offer(queue, ("log", {"i": 1}))
    EventService._offer(queue, ("log", {"i": 2}))
    EventService._offer(queue, ("log", {"i": 3}))  # full -> drops oldest (i=1)

    assert queue.qsize() == 2
    assert queue.get_nowait()[1]["i"] == 2
    assert queue.get_nowait()[1]["i"] == 3


def test_publish_never_blocks_on_full_client_queue() -> None:
    service = _service(sse_client_queue_maxsize=2)

    async def scenario() -> None:
        state = service.get_state("s1")
        queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        state.queues[1] = queue
        state.clients.add(1)

        # Far more events than the buffer; must complete without blocking.
        for i in range(50):
            await asyncio.wait_for(service.publish("s1", "log", {"i": i}), timeout=1.0)

        assert queue.qsize() <= 2

    asyncio.run(scenario())


def test_idle_states_are_evicted_at_cap() -> None:
    service = _service(sse_max_tracked_sessions=2)

    async def scenario() -> None:
        await service.publish("a", "log", {})  # idle state a
        await service.publish("b", "log", {})  # idle state b
        await service.publish("c", "log", {})  # exceeds cap -> evict an idle state

        assert len(service._states) <= 2

    asyncio.run(scenario())


def test_active_client_state_is_not_evicted() -> None:
    service = _service(sse_max_tracked_sessions=1)

    async def scenario() -> None:
        active = service.get_state("active")
        active.clients.add(99)  # has a connected client
        service.get_state("other")  # triggers eviction; must not drop "active"

        assert "active" in service._states

    asyncio.run(scenario())
