from __future__ import annotations

import asyncio

from app.core.tasks import BackgroundTaskRegistry


def test_spawn_tracks_then_releases_on_completion() -> None:
    async def scenario() -> None:
        registry = BackgroundTaskRegistry()
        started = asyncio.Event()
        release = asyncio.Event()

        async def work() -> None:
            started.set()
            await release.wait()

        task = registry.spawn(work(), name="unit-work")
        await started.wait()
        # While in-flight the registry holds a strong reference.
        assert registry.active_count == 1

        release.set()
        await task
        # done_callback runs on the next loop tick; yield to let it fire.
        await asyncio.sleep(0)
        assert registry.active_count == 0

    asyncio.run(scenario())


def test_failed_task_is_logged_and_does_not_propagate() -> None:
    async def scenario() -> None:
        registry = BackgroundTaskRegistry()

        async def boom() -> None:
            raise RuntimeError("background failure")

        task = registry.spawn(boom(), name="unit-boom")
        # The exception is retrieved by the done-callback, so awaiting must not
        # raise an unretrieved-exception warning and the registry must clear it.
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        assert registry.active_count == 0
        assert task.exception() is not None

    asyncio.run(scenario())


def test_drain_awaits_inflight_tasks() -> None:
    async def scenario() -> None:
        registry = BackgroundTaskRegistry()
        done = asyncio.Event()

        async def work() -> None:
            await asyncio.sleep(0.01)
            done.set()

        registry.spawn(work(), name="unit-drain")
        await registry.drain()
        assert done.is_set()
        assert registry.active_count == 0

    asyncio.run(scenario())
