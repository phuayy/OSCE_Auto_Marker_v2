from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from app.core.config import Settings
from app.domain.events import SessionEventState

logger = logging.getLogger(__name__)

SseEvent = tuple[str, dict[str, Any]]


class EventService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._states: dict[str, SessionEventState] = {}
        self._client_counter = 0

    @property
    def enabled(self) -> bool:
        """Whether anything published here can reach a client.

        Per-session SSE is off by default (``SESSION_SSE_ENABLED``): the browser
        drives live state from the change feed instead. Producers ask this
        *before* building a per-line callback, because the cost of the dead path
        is not the no-op ``publish`` at the end of it — it is one
        ``run_coroutine_threadsafe`` hop per line of subprocess output, from the
        reader thread into the event loop, for a WhisperX run that emits
        thousands of them.
        """
        return bool(self.settings.session_sse_enabled)

    def log_sink(self, session_id: str, source: str) -> Callable[[str, str], Awaitable[None]] | None:
        """A ``CommandRunner.on_output`` callback that forwards each line as a
        ``log`` event — or ``None`` when nothing would receive it.

        ``None`` is the point: ``CommandRunner`` skips the whole cross-thread
        hand-off when there is no callback, so a disabled stream costs nothing
        per line instead of costing a scheduled coroutine per line.

        Only for handlers that *just* log. A handler that also parses progress
        (WhisperX, the person detector) must stay installed whatever SSE is
        doing, and guards its own publish call.
        """
        if not self.enabled:
            return None

        async def forward(stream: str, text: str) -> None:
            await self.publish(session_id, "log", {"source": f"{source}-{stream}", "message": text})

        return forward

    def get_state(self, session_id: str) -> SessionEventState:
        if session_id not in self._states:
            self._evict_idle_states_if_needed()
            self._states[session_id] = SessionEventState()
        return self._states[session_id]

    async def publish(self, session_id: str, event_name: str, payload: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        state = self.get_state(session_id)
        event_payload = {
            "id": state.event_counter + 1,
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            **(payload or {}),
        }
        state.event_counter += 1
        state.history.append((event_name, event_payload))
        if len(state.history) > self.settings.session_event_history_limit:
            state.history.pop(0)
        if event_name == "status" and str(event_payload.get("code", "")) in {"completed", "failed"}:
            state.finalized = True
        # Non-blocking fan-out: a stalled client must never block the producer
        # pipeline, so we drop the oldest buffered event instead of awaiting.
        for queue in list(state.queues.values()):
            self._offer(queue, (event_name, event_payload))

    @staticmethod
    def _offer(queue: "asyncio.Queue[SseEvent]", item: SseEvent) -> None:
        try:
            queue.put_nowait(item)
            return
        except asyncio.QueueFull:
            pass
        # Drop the oldest event to make room for the newest (bounded memory).
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            pass

    def _evict_idle_states_if_needed(self) -> None:
        cap = self.settings.sse_max_tracked_sessions
        if cap <= 0 or len(self._states) < cap:
            return
        for session_id, state in list(self._states.items()):
            if not state.clients:
                self._states.pop(session_id, None)
                logger.debug(
                    "Evicted idle SSE state.",
                    extra={"trace_id": session_id, "stage": "sse_state_eviction"},
                )
                if len(self._states) < cap:
                    return

    async def connect(self, session_id: str):
        state = self.get_state(session_id)
        self._client_counter += 1
        client_id = self._client_counter
        maxsize = max(0, self.settings.sse_client_queue_maxsize)
        queue: asyncio.Queue[SseEvent] = asyncio.Queue(maxsize=maxsize)
        state.clients.add(client_id)
        state.queues[client_id] = queue

        async def generator():
            try:
                yield "retry: 1000\n\n"
                for event_name, payload in state.history:
                    yield self.format_event(event_name, payload)
                yield self.format_event(
                    "connected",
                    {"sessionId": session_id, "message": "Live session event stream connected."},
                )
                while True:
                    event_name, payload = await queue.get()
                    yield self.format_event(event_name, payload)
            finally:
                state.clients.discard(client_id)
                state.queues.pop(client_id, None)
                self.close_if_idle(session_id)

        return generator()

    @staticmethod
    def format_event(event_name: str, payload: dict[str, Any]) -> str:
        return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def close_if_idle(self, session_id: str) -> None:
        state = self._states.get(session_id)
        if state and not state.clients and state.finalized:
            self._states.pop(session_id, None)
