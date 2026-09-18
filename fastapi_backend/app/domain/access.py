"""Who may change a record someone created: its creator, or an admin.

Every marker still *sees* every session — there is no read-side permission
matrix — but a session, and the job or upload row a run of it produces, may
only be *changed* by whoever created it or by an admin. This is the one gate,
applied generically to any record carrying a ``createdBy`` provenance
snapshot (see ``app.domain.actors``) rather than written once for sessions
and again for uploads.

A record with no snapshot — one written before accounts existed, or by an
internal caller with no actor — has no owner to defer to and stays mutable by
anyone, deliberately: a migrated deployment's old data must never lock
everyone out, and the job queue's own writes (``actor=None``) are not a gap in
the HTTP surface, which always has a route dependency in front of it.
"""

from __future__ import annotations

from typing import Any

from app.core.exceptions import AppError
from app.domain.actors import Actor, provenance_user_id


def record_owner(record: dict[str, Any]) -> str | None:
    """The user id recorded as ``record``'s creator, or None for a legacy or
    system-created row."""
    return provenance_user_id(record)


def may_mutate(record: dict[str, Any], actor: Actor | None) -> bool:
    """Whether ``actor`` may change ``record``."""
    if actor is None or actor.is_admin:
        return True
    owner = record_owner(record)
    return owner is None or owner == actor.user_id


def ensure_may_mutate(record: dict[str, Any], actor: Actor | None, *, subject: str = "record") -> None:
    """Raise a 403 :class:`AppError` unless ``actor`` may change ``record``."""
    if not may_mutate(record, actor):
        raise AppError(
            f"Only the {subject}'s creator or an administrator may change it.",
            status_code=403,
        )
