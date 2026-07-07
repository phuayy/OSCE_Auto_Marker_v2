from __future__ import annotations

from pydantic import BaseModel


class ErrorResponse(BaseModel):
    error: str


class HealthResponse(BaseModel):
    ok: bool
    service: str
