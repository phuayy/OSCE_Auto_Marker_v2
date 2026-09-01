from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)


class BackgroundTaskRegistry:
    """Owns strong references to fire-and-forget :class:`asyncio.Task` objects.

    CPython's event loop keeps only a *weak* reference to tasks created with
    :func:`asyncio.create_task`. Without an external strong reference the task
    may be garbage-collected mid-execution, silently aborting the work. This
    registry holds a strong reference for each task's lifetime, removes it on
    completion, and logs any unhandled exception so failures are never swallowed.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        """Schedule ``coro`` as a tracked background task and return the task."""
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "Background task failed.",
                extra={"task_name": task.get_name(), "stage": "background_task"},
                exc_info=error,
            )

    @property
    def active_count(self) -> int:
        """Number of tasks currently being tracked (in-flight)."""
        return len(self._tasks)

    async def drain(self) -> None:
        """Await all in-flight tasks; used for graceful shutdown."""
        pending = list(self._tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def cancel_all(self) -> None:
        """Cancel every in-flight task and wait for it to unwind.

        The counterpart to :meth:`drain` for work that must not delay shutdown
        — a model download, say — where finishing is optional and the next boot
        can resume it.
        """
        pending = list(self._tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
