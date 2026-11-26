"""One action applied to several sessions, each judged on its own.

``POST /api/sessions/bulk`` exists so a marker can re-run or delete a handful
of sessions without one request (and one hourly-budget unit) per row. It adds
no behaviour of its own: every item goes through the same service call the
single-session route makes, behind the same creator-or-admin gate
(``app.domain.access``), and an item's refusal is that item's result — a
missing session, someone else's session or a session already in flight never
stops the rest.

Actions are a registry (``BULK_SESSION_ACTIONS``). A new one is one entry: the
handler, whether it spends the expensive-operation budget (once per request,
not per item) and, if it takes any, an ``options`` model.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from app.domain.access import ensure_may_mutate
from app.core.error_mapping import classify_error
from app.domain.actors import Actor
from app.schemas.sessions import BulkSessionResult, BulkSessionsSummary, BulkSessionsResponse
from app.services.session_maintenance_service import SessionMaintenanceService
from app.services.session_service import SessionService

logger = logging.getLogger(__name__)

MAX_BULK_SESSIONS = 100

BulkHandler = Callable[["SessionBulkService", str, BaseModel | None], Awaitable[object]]


@dataclass(frozen=True)
class BulkAction:
    handler: BulkHandler
    # Whether one request of this action spends one unit of the account's
    # hourly expensive-operation budget (what the single route's
    # `require_expensive_operation` charges per call). Charged once per
    # request, so a batch of 11 is not refused by a budget of 10.
    charges_budget: bool
    options_model: type[BaseModel] | None = None
    # Whether the action on a recording also removes its clip children, so a
    # child ticked beside its recording is covered rather than a failure.
    cascades_to_children: bool = False


async def _rerun(service: SessionBulkService, session_id: str, _options: BaseModel | None) -> object:
    return await service.maintenance.rerun_session(session_id)


async def _delete(service: SessionBulkService, session_id: str, _options: BaseModel | None) -> object:
    # Children of a long recording go with it, exactly as the single delete.
    return await service.maintenance.delete_session(session_id)


BULK_SESSION_ACTIONS: dict[str, BulkAction] = {
    "rerun": BulkAction(_rerun, charges_budget=True),
    # The single delete charges nothing, so neither does a batch of them.
    "delete": BulkAction(_delete, charges_budget=False, cascades_to_children=True),
}

COVERED_BY_PARENT_NOTE = "Deleted with its recording."

_CODES = {403: "forbidden", 404: "not_found", 409: "conflict", 429: "rate_limited"}


def _code_for(status_code: int) -> str:
    return _CODES.get(status_code, "failed" if status_code >= 500 else "rejected")


@dataclass(frozen=True)
class BulkPlan:
    """A validated request: nothing has run, nothing has been charged."""

    name: str
    action: BulkAction
    session_ids: tuple[str, ...]
    options: BaseModel | None

    @property
    def charges_budget(self) -> bool:
        return self.action.charges_budget


class SessionBulkService:
    def __init__(self, sessions: SessionService, maintenance: SessionMaintenanceService) -> None:
        self.sessions = sessions
        self.maintenance = maintenance

    @staticmethod
    def prepare(action: str, session_ids: list[str], options: dict[str, Any] | None) -> BulkPlan:
        """Validate a request before anything is charged or changed.

        Raises ``ValueError`` with a message meant for the caller.
        """
        spec = BULK_SESSION_ACTIONS.get(action)
        if spec is None:
            raise ValueError(f"Unknown bulk action '{action}'. Expected one of: {', '.join(BULK_SESSION_ACTIONS)}.")
        ids = tuple(str(item).strip() for item in session_ids)
        if not 1 <= len(ids) <= MAX_BULK_SESSIONS:
            raise ValueError(f"`sessionIds` must name between 1 and {MAX_BULK_SESSIONS} sessions.")
        if not all(ids):
            raise ValueError("`sessionIds` must not contain empty ids.")
        if len(set(ids)) != len(ids):
            raise ValueError("`sessionIds` must not contain duplicates.")
        parsed: BaseModel | None = None
        if spec.options_model is None:
            if options:
                raise ValueError(f"The '{action}' action takes no options.")
        else:
            try:
                parsed = spec.options_model.model_validate(options or {})
            except ValidationError as error:
                first = error.errors()[0]
                raise ValueError(f"Invalid options for '{action}': {first.get('msg', 'invalid value')}.") from error
        return BulkPlan(action, spec, ids, parsed)

    async def run(self, plan: BulkPlan, actor: Actor | None) -> BulkSessionsResponse:
        """Apply the action to each session in request order, one at a time.

        Sequential on purpose: each item is a short transaction and a delete
        touches files, so running them together would only contend.
        """
        parents = await self._recorded_parents(plan)
        done: set[str] = set()
        results: list[BulkSessionResult] = []
        for session_id in plan.session_ids:
            result = await self._run_one(plan, session_id, actor)
            if not result.ok and result.status == 404 and parents.get(session_id) in done:
                # Its recording went earlier in this request and took it along.
                result = BulkSessionResult(session_id=session_id, ok=True, status=200, note=COVERED_BY_PARENT_NOTE)
            elif result.ok:
                done.add(session_id)
            results.append(result)
        succeeded = sum(1 for result in results if result.ok)
        return BulkSessionsResponse(
            results=results, summary=BulkSessionsSummary(ok=succeeded, failed=len(results) - succeeded),
        )

    async def _recorded_parents(self, plan: BulkPlan) -> dict[str, str]:
        """For an action that cascades, each requested session's parent, read
        before anything is removed (a deleted child can no longer be asked)."""
        if not plan.action.cascades_to_children:
            return {}
        parents: dict[str, str] = {}
        for session_id in plan.session_ids:
            try:
                parent = (await self.sessions.read(session_id)).get("parentSessionId")
            except FileNotFoundError:
                continue
            if parent:
                parents[session_id] = str(parent)
        return parents

    async def _run_one(self, plan: BulkPlan, session_id: str, actor: Actor | None) -> BulkSessionResult:
        try:
            session = await self.sessions.read(session_id)  # FileNotFoundError -> 404
            # The route has no {session_id} for `require_session_owner` to read,
            # so the same gate runs here, per item.
            ensure_may_mutate(session, actor, subject="session")
            await plan.action.handler(self, session_id, plan.options)
        except Exception as error:  # noqa: BLE001 - one item's failure is that item's result
            outcome = classify_error(
                error, fallback_message="Failed to complete this action.", not_found_message="Session not found.",
            )
            return BulkSessionResult(
                session_id=session_id, ok=False, status=outcome.status_code,
                error=outcome.message, code=_code_for(outcome.status_code),
            )
        return BulkSessionResult(session_id=session_id, ok=True, status=200)
