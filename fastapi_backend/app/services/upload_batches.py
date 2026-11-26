"""Allowances that let one account upload many recordings under one charge.

``POST /api/uploads/initiate`` is charged against the account's hourly
expensive-operation budget (``RATE_LIMIT_RERUN_PER_HOUR``), so a loop over an
exam day's recordings is refused after the first handful. The browser instead
opens a *batch* for N recordings — charged once, when it is opened — and sends
its id with each initiate; every initiate then spends one unit of the batch
rather than one of the hourly budget. Nothing else about an upload changes: the
concurrent-uploads cap is still enforced by ``AsyncUploadService.initiate``.

The store is in process, keyed by account, like the rate limiters it sits
beside (``core/rate_limit.py``): a second API process would not know a batch
the first opened. That is the same single-process scope ``local`` already
requires, and a batch is only a few hours of convenience, so a restart simply
makes the browser open a new one.
"""
from __future__ import annotations

import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from app.core.exceptions import AppError

MIN_BATCH_SIZE = 2


@dataclass(frozen=True)
class UploadBatch:
    batch_id: str
    count: int
    remaining: int
    expires_at: str


@dataclass
class _Allowance:
    batch_id: str
    count: int
    remaining: int
    expires_at: float  # epoch seconds


class UploadBatchAllowances:
    """Per-account batch allowances. Synchronous and guarded by a threading
    lock, because the dependencies that call it are plain functions FastAPI
    runs on worker threads."""

    def __init__(
        self,
        *,
        max_size: int,
        ttl_seconds: int,
        max_open_per_user: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._max_size = max(MIN_BATCH_SIZE, max_size)
        self._ttl = max(1, ttl_seconds)
        self._max_open = max(1, max_open_per_user)
        self._clock = clock
        self._by_user: dict[str, list[_Allowance]] = {}
        self._lock = threading.Lock()

    def open(self, user_id: str, count: int) -> UploadBatch:
        if count < MIN_BATCH_SIZE or count > self._max_size:
            raise AppError(
                f"A batch holds between {MIN_BATCH_SIZE} and {self._max_size} recordings.",
                status_code=422,
            )
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            # A fully spent batch is only kept so a stray extra initiate gets a
            # clear message; it must not count against the open-batch cap, or a
            # finished morning would block the afternoon's batch.
            open_batches = [a for a in self._by_user.get(user_id, []) if a.remaining > 0]
            if len(open_batches) >= self._max_open:
                raise AppError(
                    f"You already have {self._max_open} batches open. Finish one before starting another.",
                    status_code=429,
                )
            allowance = _Allowance(secrets.token_hex(16), count, count, now + self._ttl)
            self._by_user[user_id] = [*open_batches, allowance]
            return self._view(allowance)

    def consume(self, user_id: str, batch_id: str) -> None:
        with self._lock:
            allowance = self._find_locked(user_id, batch_id)
            if allowance.remaining <= 0:
                raise AppError(
                    "This batch has no uploads left. Start a new batch to add more recordings.",
                    status_code=409,
                )
            allowance.remaining -= 1

    def release(self, user_id: str, batch_id: str) -> None:
        """Give back a unit whose initiate did not create an upload. A batch
        that has meanwhile expired is simply gone: there is nothing to refund."""
        with self._lock:
            try:
                allowance = self._find_locked(user_id, batch_id)
            except AppError:
                return
            allowance.remaining = min(allowance.count, allowance.remaining + 1)

    def _find_locked(self, user_id: str, batch_id: str) -> _Allowance:
        now = self._clock()
        self._prune_user_locked(user_id, now)
        # Compared against each of the account's few batches rather than looked
        # up by key, so the id is never matched by a short-circuiting lookup;
        # another account's id is simply not among them.
        match = None
        for allowance in self._by_user.get(user_id, []):
            if hmac.compare_digest(allowance.batch_id.encode(), str(batch_id).encode()):
                match = allowance
        if match is None:
            raise AppError(
                "This upload batch is unknown or has expired. Start a new batch.",
                status_code=409,
            )
        return match

    def _prune_user_locked(self, user_id: str, now: float) -> None:
        kept = [a for a in self._by_user.get(user_id, []) if a.expires_at > now]
        if kept:
            self._by_user[user_id] = kept
        else:
            self._by_user.pop(user_id, None)

    def _prune_locked(self, now: float) -> None:
        for user_id in list(self._by_user):
            self._prune_user_locked(user_id, now)

    @staticmethod
    def _view(allowance: _Allowance) -> UploadBatch:
        expires = datetime.fromtimestamp(allowance.expires_at, tz=timezone.utc)
        return UploadBatch(
            batch_id=allowance.batch_id,
            count=allowance.count,
            remaining=allowance.remaining,
            expires_at=expires.isoformat().replace("+00:00", "Z"),
        )
