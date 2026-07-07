from __future__ import annotations

import asyncio
import json
import logging
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

    def get_state(self, session_id: str) -> SessionEventState:
        if session_id not in self._states:
            self._evict_idle_states_if_needed()
            self._states[session_id] = SessionEventState()
        return self._states[session_id]

    async def publish(self, session_id: str, event_name: str, payload: dict[str, Any] | None = None) -> None:
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
