"""The session-store contract, for test doubles.

Production code changes a session through ``SessionService.update`` (read →
mutate → write, replayed on conflict). A fake that only implements ``read`` and
``write`` can borrow ``update`` from this mixin instead of re-implementing it —
which also keeps every fake's semantics identical to the real thing: the
mutator sees a fresh copy, and returning ``False`` skips the write.
"""

from __future__ import annotations

import copy
from typing import Any, Callable


class SessionUpdateMixin:
    async def update(self, session_id: str, mutate: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
        session = await self.read(session_id)  # type: ignore[attr-defined]
        if mutate(session) is False:
            return session
        await self.write(session)  # type: ignore[attr-defined]
        return copy.deepcopy(session)
