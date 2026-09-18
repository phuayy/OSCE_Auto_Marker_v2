from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator

from app.core.asyncio_compat import (
    configure_windows_selector_event_loop_policy,
    configure_windows_signal_compatibility,
)
from app.core.config import Settings
from app.queue.hatchet_tasks import bind_worker_container, hatchet, process_job
from app.services.container import AppContainer, ContainerRole, create_container


configure_windows_selector_event_loop_policy()
configure_windows_signal_compatibility()

logger = logging.getLogger(__name__)

settings = Settings.load()


async def _redispatch_loop(container: AppContainer, interval_seconds: int) -> None:
    """Periodically re-scan for queued jobs that were never dispatched (or whose
    dispatch went stale) and dispatch them.

    The API process dispatches jobs at enqueue time, but if that ever fails
    (e.g. its Hatchet client is unavailable), the job would otherwise sit
    ``queued`` with no Hatchet metadata until the next worker restart. Running
    the recovery on an interval — from the worker, whose Hatchet client is known
    good — makes queueing continuous without a restart.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await container.jobs.redispatch_stale_hatchet_jobs()
        except Exception:
            logger.exception("Periodic Hatchet redispatch failed; retrying next interval.")


async def _worker_lifespan() -> AsyncGenerator[None, None]:
    """Startup/shutdown hook that runs inside the Hatchet worker's own event loop.

    Using Hatchet's ``lifespan`` parameter (instead of a separate
    ``asyncio.run()`` before ``worker.start()``) ensures that every async
    resource — gRPC channels, DB connections, asyncio tasks — is created and
    destroyed in the same event loop as the worker itself.  The previous
    pattern of calling ``asyncio.run(initialize_worker_runtime(...))`` closed
    the first loop before the worker started its own loop, which left the
    Hatchet client's internal gRPC channel in a stale state and caused the
    worker to silently fail to receive dispatched jobs.
    """
    # WORKER role: no schema migration, no seed data, none of the API's upload
    # sweeps (which would fail an upload the API is assembling right now), and
    # no startup job recovery — Hatchet owns dispatch. Bound once so every job
    # this process runs shares these connections, caches and the GPU lease.
    container = create_container(settings)
    redispatch_task: asyncio.Task[None] | None = None
    try:
        await container.startup(role=ContainerRole.WORKER)
        bind_worker_container(container)
        logger.info("Hatchet worker runtime initialized.")
        # Re-dispatch any jobs whose Hatchet schedule window may have expired
        # while the worker was down (e.g., server restart, crash).
        await container.jobs.redispatch_stale_hatchet_jobs()
        interval = settings.hatchet_redispatch_interval_seconds
        if interval > 0:
            redispatch_task = asyncio.create_task(_redispatch_loop(container, interval))
            logger.info("Hatchet periodic redispatch enabled (every %ds).", interval)
        yield
    finally:
        if redispatch_task is not None:
            redispatch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await redispatch_task
        bind_worker_container(None)
        await container.shutdown()
        logger.info("Hatchet worker runtime shut down.")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    worker = hatchet.worker(
        settings.hatchet_worker_name,
        slots=max(1, settings.job_worker_concurrency),
        workflows=[process_job],
        lifespan=_worker_lifespan,
    )
    worker.start()


if __name__ == "__main__":
    main()
