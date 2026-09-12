"""Leases on machine resources that the job queue does not know about.

``JOB_WORKER_CONCURRENCY`` bounds how many *jobs* run at once, and a job is
mostly network-bound: two LLM scorers waiting on a vendor cost nothing to
overlap. The one thing they must not overlap on is the accelerator. Two
``process_session`` jobs each load a WhisperX ``large-v3`` (and a long upload
adds RT-DETR on top) into the same 4–6 GB card, the second load dies of CUDA
OOM, and the queue records a ``TranscriptionResourceError`` — deliberately
non-retryable, because on a *real* memory shortage a retry fails identically.
Contention the queue itself created is not that case, and should never reach
that verdict.

A :class:`ResourceLease` is an ``asyncio.Semaphore`` with a name, held only
around the step that needs the resource, so the rest of the job keeps
overlapping. It is per process: a Hatchet deployment with several workers on
one machine bounds the machine through the worker's own ``slots`` instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger(__name__)


class ResourceLease:
    """A named, bounded lease. ``slots <= 0`` means unbounded: ``hold`` is a no-op."""

    def __init__(self, slots: int, name: str) -> None:
        self.name = str(name)
        self.slots = max(0, int(slots))
        self._semaphore = asyncio.Semaphore(self.slots) if self.slots > 0 else None
        self._in_use = 0
        self._waiting = 0

    @classmethod
    def unbounded(cls, name: str = "unbounded") -> "ResourceLease":
        return cls(0, name)

    @property
    def bounded(self) -> bool:
        return self._semaphore is not None

    @property
    def in_use(self) -> int:
        return self._in_use

    @property
    def waiting(self) -> int:
        return self._waiting

    @contextlib.asynccontextmanager
    async def hold(self, label: str, trace_id: str | None = None) -> AsyncIterator[None]:
        """Hold one slot for the duration of the block.

        A wait is logged once when it ends, with how long it lasted: on a
        one-GPU host the wait *is* the queue position, and an operator reading
        "transcription waited 812 s for gpu" learns why a run took twice as
        long without inferring it from timestamps.
        """
        if self._semaphore is None:
            yield
            return
        started = time.monotonic()
        self._waiting += 1
        try:
            waited_log_due = self._semaphore.locked()
            await self._semaphore.acquire()
        finally:
            self._waiting -= 1
        waited = time.monotonic() - started
        if waited_log_due:
            logger.info(
                "%s waited %.1fs for the %s lease.",
                label,
                waited,
                self.name,
                extra={"trace_id": trace_id or "", "stage": f"{self.name}_lease", "waited_seconds": round(waited, 1)},
            )
        self._in_use += 1
        try:
            yield
        finally:
            self._in_use -= 1
            self._semaphore.release()

    def stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "slots": self.slots or None,
            "inUse": self._in_use,
            "waiting": self._waiting,
        }


__all__ = ["ResourceLease"]
