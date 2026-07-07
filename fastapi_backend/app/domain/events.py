from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionEventState:
    clients: set[int] = field(default_factory=set)
    queues: dict[int, asyncio.Queue[tuple[str, dict[str, Any]]]] = field(default_factory=dict)
    history: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    event_counter: int = 0
    finalized: bool = False


__all__ = ["SessionEventState"]
