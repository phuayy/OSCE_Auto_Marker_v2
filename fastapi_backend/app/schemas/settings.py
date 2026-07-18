from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class UpdateSettingsRequest(BaseModel):
    # extra="forbid" so an unknown key 422s instead of being silently dropped —
    # a typo'd setting name should be loud, not a no-op.
    model_config = ConfigDict(extra="forbid")

    llmTranscriptPreprocess: bool
