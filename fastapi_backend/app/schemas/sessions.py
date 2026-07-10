from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


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
    boundaries: list[float] = Field(default_factory=list, max_length=200)
    labels: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("boundaries")
    @classmethod
    def boundaries_must_be_finite_and_non_negative(cls, value: list[float]) -> list[float]:
        for item in value:
            if not math.isfinite(item) or item < 0:
                raise ValueError("Clip boundaries must be non-negative, finite seconds.")
        return value

    @field_validator("labels")
    @classmethod
    def trim_labels(cls, value: list[str]) -> list[str]:
        return [str(item or "").strip()[:80] for item in value]


class RecropClipRequest(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)

    @field_validator("start", "end")
    @classmethod
    def must_be_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Clip bounds must be finite seconds.")
        return value

    @model_validator(mode="after")
    def end_must_follow_start(self) -> "RecropClipRequest":
        if self.end <= self.start:
            raise ValueError("`end` must be greater than `start`.")
        return self


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
