from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    # Kept as ``username`` on the wire for compatibility; the value may be a
    # username or an email address, and the server matches either.
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


class IdentityFields(BaseModel):
    """What a client is allowed to know about the account it holds a token for."""

    userId: str
    username: str
    role: str
    displayName: str = ""
    email: str = ""


class AuthResponse(IdentityFields):
    token: str
    expiresAt: int


class AuthMeResponse(IdentityFields):
    expiresAt: int


class StreamTicketResponse(BaseModel):
    ticket: str
    expiresAt: int


class LogoutResponse(BaseModel):
    revoked: bool
