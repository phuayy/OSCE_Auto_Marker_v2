from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)

    @field_validator("username", mode="before")
    @classmethod
    def username_must_not_be_blank(cls, value: Any) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("Username is required.")
        return cleaned

    @field_validator("password")
    @classmethod
    def password_must_not_be_blank(cls, value: str) -> str:
        if not value:
            raise ValueError("Password is required.")
        return value


class AuthResponse(BaseModel):
    token: str
    expiresAt: int
    username: str


class AuthMeResponse(BaseModel):
    username: str
    expiresAt: int


class StreamTicketResponse(BaseModel):
    ticket: str
    expiresAt: int


class LogoutResponse(BaseModel):
    revoked: bool
