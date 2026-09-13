"""One event-service double for the whole suite.

Thirteen modules each kept their own two-line ``FakeEvents``/``CapturingEvents``
/``_Events``. They agreed on ``publish`` and on nothing else, so the day
``EventService`` grew ``log_sink`` — the method producers call *instead of*
building a per-line publish callback — every one of them was a double that no
longer matched the thing it doubles.

This is that double, once. It records what was published and answers the rest of
the contract for real, so a producer that asks "is anyone listening?" gets a
truthful answer here too.
"""

from __future__ import annotations

from typing import Any


class RecordingEvents:
    """Records published events; speaks the whole ``EventService`` surface.

    ``enabled`` defaults to True so tests exercise the live path (a per-line log
    sink is handed out, and every publish is recorded). Construct with
    ``RecordingEvents(enabled=False)`` to assert what a deployment with
    ``SESSION_SSE_ENABLED=false`` actually does: no sink, nothing published.
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self.items: list[tuple[str, str, dict[str, Any]]] = []
        self.enabled = enabled

    @property
    def published(self) -> list[tuple[str, str, dict[str, Any]]]:
        """Alias kept for the doubles that spelled it this way."""
        return self.items

    async def publish(self, session_id: str, event_name: str, payload: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        self.items.append((str(session_id), str(event_name), payload or {}))

    def log_sink(self, session_id: str, source: str):
        if not self.enabled:
            return None

        async def forward(stream: str, text: str) -> None:
            await self.publish(session_id, "log", {"source": f"{source}-{stream}", "message": text})

        return forward
