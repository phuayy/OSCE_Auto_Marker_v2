from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class CommunicationRubricSaveRequest(BaseModel):
    """Body of ``PUT /api/admin/communication-rubric``.

    ``rubric`` is deliberately an untyped object: its structure is checked by
    ``domain.communication_rubric.validate_edited_rubric`` so every problem is
    reported at once rather than only pydantic's first.
    """

    rubric: dict[str, Any]
    draftId: str | None = None
