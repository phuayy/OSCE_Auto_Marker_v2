"""When a person last did something on this deployment.

The idle shutdown (``services/idle_shutdown_service.py``) needs one fact from
the HTTP layer: how long since a signed-in user last *acted*. "Signed in" alone
cannot answer it: a bearer token is self-contained and lives for hours, so the
server never learns that a tab was closed. A request can, as long as the
requests an idle tab makes on its own are not counted — otherwise one forgotten
browser window would keep a GPU machine running all night.
"""

from __future__ import annotations

import time
from collections.abc import Callable


# Requests a browser tab makes on its own, with nobody at the keyboard: the
# change stream and its fallback poll, the notification bell's poll, the
# ticket the stream reconnects with, and every health probe. Anything else a
# signed-in client sends is a person doing something.
_PASSIVE_PREFIXES: tuple[str, ...] = (
    "/api/events",
    "/api/notifications",
    "/api/health",
    "/api/auth/stream-ticket",
)


def counts_as_activity(method: str, path: str) -> bool:
    """Whether an authenticated request is a person acting rather than a tab polling."""
    if method.upper() == "OPTIONS":
        return False
    if method.upper() == "GET" and any(
        path == prefix or path.startswith(prefix + "/") for prefix in _PASSIVE_PREFIXES
    ):
        return False
    return True


class ActivityTracker:
    """The time of the last activity, on a monotonic clock.

    Starts "just active": the process booting is itself the result of someone
    asking for it (the start gate), so a fresh VM gets the full idle window
    before it may switch itself off again.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last = clock()

    def record(self) -> None:
        self._last = self._clock()

    def idle_seconds(self) -> float:
        return max(0.0, self._clock() - self._last)
