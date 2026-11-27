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
    # The id of the active version the editor was opened on. Required: the save
    # is refused with 409 when it is no longer the active one.
    baseVersionId: str
    draftId: str | None = None
