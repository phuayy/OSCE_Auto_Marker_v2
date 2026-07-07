from __future__ import annotations

import threading
import time


class TokenRevocationRegistry:
    """Process-local registry of revoked token IDs (logout / kill-switch).

    Stores ``token_id -> expires_at_ms`` so a revoked token is forgotten once it
    would have expired anyway, which bounds memory growth. It is intentionally
    in-process: the API server is the only component that verifies user tokens.
    A multi-process deployment needing shared revocation should back this with
    Redis/DB behind the same interface.
    """

    def __init__(self) -> None:
        self._revoked: dict[str, int] = {}
        self._lock = threading.Lock()

    def revoke(self, token_id: str, expires_at_ms: int) -> None:
        if not token_id:
            return
        with self._lock:
            self._prune_locked()
            self._revoked[token_id] = int(expires_at_ms)

    def is_revoked(self, token_id: str) -> bool:
        if not token_id:
            return False
        with self._lock:
            expires_at = self._revoked.get(token_id)
            if expires_at is None:
                return False
            if expires_at <= self._now_ms():
                self._revoked.pop(token_id, None)
                return False
            return True

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._revoked)

    def _prune_locked(self) -> None:
        now = self._now_ms()
        for token_id in [tid for tid, exp in self._revoked.items() if exp <= now]:
            self._revoked.pop(token_id, None)

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)
