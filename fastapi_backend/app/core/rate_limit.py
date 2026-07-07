from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from app.core.exceptions import AppError


# Cap on the number of distinct keys retained before an opportunistic sweep, so
# a flood of unique source IPs cannot grow the table without bound.
_MAX_TRACKED_KEYS = 4096


class FixedWindowRateLimiter:
    """Per-key fixed-window rate limiter (in-process, thread-safe).

    Throttles brute-force attempts against an endpoint (e.g. login) by source
    key (typically client IP). It is intentionally in-process; a multi-process
    deployment should back this with a shared store (Redis) behind the same
    ``check`` interface.
    """

    def __init__(self, *, max_attempts: int, window_seconds: int) -> None:
        self._max = max(1, max_attempts)
        self._window = max(1, window_seconds)
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        """Record an attempt for ``key``; raise :class:`AppError` (429) if exceeded."""
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self._max:
                retry_after = max(1, int(hits[0] + self._window - now) + 1)
                raise AppError(
                    f"Too many attempts. Please wait {retry_after}s and try again.",
                    status_code=429,
                )
            hits.append(now)
            if len(self._hits) > _MAX_TRACKED_KEYS:
                self._prune_locked(cutoff)

    def _prune_locked(self, cutoff: float) -> None:
        for key in [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]:
            self._hits.pop(key, None)
