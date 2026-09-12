"""Per-key asyncio locks that disappear when nobody holds them.

Several services serialise work on one record at a time — the parts of one
upload, the completion of one upload — without wanting to serialise every
record behind one lock. A plain ``dict[str, asyncio.Lock]`` does that but leaks
an entry per key forever; this keeps the map bounded by releasing a key's lock
once its last holder lets go.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator


class KeyedLocks:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._waiters: dict[str, int] = {}

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._waiters[key] = self._waiters.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            remaining = self._waiters.get(key, 1) - 1
            if remaining <= 0:
                self._waiters.pop(key, None)
                # Only drop the entry if it is still the lock we took; a
                # concurrent holder that arrived after our count hit zero would
                # have created a fresh one.
                if self._locks.get(key) is lock and not lock.locked():
                    self._locks.pop(key, None)
            else:
                self._waiters[key] = remaining

    def __len__(self) -> int:
        return len(self._locks)


__all__ = ["KeyedLocks"]
