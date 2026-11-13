from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.domain.enums import ClipKind


class RenameSessionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name", mode="before")
    @classmethod
    def trim_non_empty_name(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("Session name is required.")
        return cleaned[:80]


class ManualClipsRequest(BaseModel):
    boundaries: list[Annotated[float, Field(ge=0, allow_inf_nan=False)]] = Field(
        default_factory=list, max_length=200
    )
    labels: list[str] = Field(default_factory=list, max_length=200)
    # Per-segment kinds, positional (segment i = between boundary i-1 and i).
    # Optional: omitted/short lists default to "session" segments.
    kinds: list[ClipKind] = Field(default_factory=list, max_length=201)

    @field_validator("labels")
    @classmethod
    def trim_labels(cls, value: list[str]) -> list[str]:
        return [str(item or "").strip()[:80] for item in value]

    @field_validator("kinds", mode="before")
    @classmethod
    def normalize_kinds(cls, value: list[str]) -> list[str]:
        normalized = [str(item or "").strip().lower() for item in value]
        invalid = sorted({item for item in normalized if item not in {"session", "intermission"}})
        if invalid:
            raise ValueError(f"Segment kinds must be 'session' or 'intermission' (got: {', '.join(invalid)}).")
        return normalized


class RecropClipRequest(BaseModel):
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def end_must_follow_start(self) -> "RecropClipRequest":
        if self.end <= self.start:
            raise ValueError("`end` must be greater than `start`.")
        return self


# Upper bound on one "Run selected" batch. Far above any OSCE circuit (a long
# recording splits into tens of stations), low enough that one request cannot
# be used to queue an unbounded amount of work.
MAX_BATCH_ASSESS_CLIPS = 200


class AssessClipsRequest(BaseModel):
    """``POST /sessions/{id}/clips/assess`` — queue several clips as one action."""

    clip_ids: list[str] = Field(alias="clipIds", min_length=1, max_length=MAX_BATCH_ASSESS_CLIPS)

    model_config = {"populate_by_name": True}

    @field_validator("clip_ids", mode="after")
    @classmethod
    def dedupe_non_empty(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value:
            clip_id = str(item or "").strip()
            if clip_id and clip_id not in seen:
                seen.add(clip_id)
                cleaned.append(clip_id)
        if not cleaned:
            raise ValueError("`clipIds` must name at least one clip.")
        return cleaned


class RenameClipRequest(BaseModel):
    label: str = Field(min_length=1, max_length=80)

    @field_validator("label", mode="before")
    @classmethod
    def trim_non_empty_label(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("Label must be a string.")
        return cleaned[:80]


class PublicSessionResponse(BaseModel):
    session: dict[str, Any]


class SessionListResponse(BaseModel):
    sessions: list[dict[str, Any]]
