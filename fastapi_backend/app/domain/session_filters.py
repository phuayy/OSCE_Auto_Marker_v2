"""What the session list may be narrowed by, validated once.

``GET /api/sessions`` accepts a name search, a status set, a creator and a
workflow. They are parsed into one immutable value here so the route, the page
cache key and the SQL ``WHERE`` all read the same normalised answer: a search
typed as ``" Smith "`` and as ``"smith"`` is the same request, and a cached
unfiltered page can never be mistaken for a filtered one.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.domain.enums import Workflow
from app.domain.sessions import SessionStatus

SESSION_SEARCH_MAX_LENGTH = 100


@dataclass(frozen=True)
class SessionListFilter:
    """Normalised list filters; every field is optional and they combine with AND."""

    search: str | None = None
    statuses: tuple[str, ...] = ()
    created_by: str | None = None
    workflow: Workflow | None = None

    @classmethod
    def parse(
        cls,
        *,
        search: str | None = None,
        statuses: Iterable[str] = (),
        created_by: str | None = None,
        workflow: str | None = None,
    ) -> "SessionListFilter":
        """Validate raw request values. Raises ``ValueError`` (the caller's 422)."""
        term = (search or "").strip()
        if len(term) > SESSION_SEARCH_MAX_LENGTH:
            raise ValueError(f"Search text must be at most {SESSION_SEARCH_MAX_LENGTH} characters.")
        known = {status.value for status in SessionStatus}
        requested = {str(item).strip().lower() for item in statuses if str(item).strip()}
        unknown = sorted(requested - known)
        if unknown:
            raise ValueError(f"Unknown session status: {', '.join(unknown)}.")
        try:
            parsed_workflow = Workflow(workflow.strip().lower()) if workflow and workflow.strip() else None
        except ValueError:
            raise ValueError("Workflow must be 'standard' or 'long'.") from None
        return cls(
            # Lowercased here, not in SQL, so the cache key and the query agree.
            search=term.lower() or None,
            statuses=tuple(sorted(requested)),
            created_by=(created_by or "").strip() or None,
            workflow=parsed_workflow,
        )

    @property
    def is_empty(self) -> bool:
        return not (self.search or self.statuses or self.created_by or self.workflow)
