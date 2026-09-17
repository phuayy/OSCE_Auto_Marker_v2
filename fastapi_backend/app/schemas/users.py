"""Request shapes for account administration and the emailed-link flows.

Shape only: whether an email is already taken, whether a token is live and
whether a password satisfies the policy are decided in
``UserAdminService``, where the answer can be phrased with the account in
hand. The validators here reject what could never be right regardless of
state — a blank password, a role this build does not know.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.domain.users import (
    DISPLAY_NAME_MAX_LENGTH,
    EMAIL_MAX_LENGTH,
    PASSWORD_MAX_BYTES,
    UserRole,
    UserVocabularyError,
    normalize_display_name,
    normalize_email,
    parse_role,
)


def _password(value: Any) -> str:
    text = str(value or "")
    if not text:
        raise ValueError("A password is required.")
    # The policy proper is applied by the service; this only keeps an absurd
    # payload from reaching bcrypt.
    if len(text.encode("utf-8")) > PASSWORD_MAX_BYTES * 4:
        raise ValueError("The password is too long.")
    return text


class InviteUserRequest(BaseModel):
    email: str = Field(min_length=3, max_length=EMAIL_MAX_LENGTH)
    role: str = UserRole.MARKER.value
    displayName: str = Field(default="", max_length=DISPLAY_NAME_MAX_LENGTH)

    @field_validator("email", mode="before")
    @classmethod
    def email_shape(cls, value: Any) -> str:
        try:
            return normalize_email(value)
        except UserVocabularyError as error:
            raise ValueError(str(error)) from None

    @field_validator("role", mode="before")
    @classmethod
    def role_known(cls, value: Any) -> str:
        try:
            return parse_role(value, default=UserRole.MARKER).value
        except UserVocabularyError as error:
            raise ValueError(str(error)) from None

    @field_validator("displayName", mode="before")
    @classmethod
    def display_name_trimmed(cls, value: Any) -> str:
        return normalize_display_name(value)

    @property
    def parsed_role(self) -> UserRole:
        return UserRole(self.role)


class UpdateUserRequest(BaseModel):
    """A partial update: only the fields sent are changed."""

    role: str | None = None
    displayName: str | None = Field(default=None, max_length=DISPLAY_NAME_MAX_LENGTH)

    @field_validator("role", mode="before")
    @classmethod
    def role_known(cls, value: Any) -> str | None:
        if value is None:
            return None
        try:
            return parse_role(value).value
        except UserVocabularyError as error:
            raise ValueError(str(error)) from None

    @property
    def parsed_role(self) -> UserRole | None:
        return UserRole(self.role) if self.role else None


class AcceptInvitationRequest(BaseModel):
    password: str
    displayName: str | None = Field(default=None, max_length=DISPLAY_NAME_MAX_LENGTH)

    @field_validator("password", mode="before")
    @classmethod
    def password_present(cls, value: Any) -> str:
        return _password(value)


class PasswordResetRequest(BaseModel):
    """The public "forgot password" form. ``identifier`` is a username or email."""

    identifier: str = Field(min_length=1, max_length=EMAIL_MAX_LENGTH)

    @field_validator("identifier", mode="before")
    @classmethod
    def identifier_trimmed(cls, value: Any) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("Enter your username or email address.")
        return cleaned


class PasswordResetConfirmRequest(BaseModel):
    password: str

    @field_validator("password", mode="before")
    @classmethod
    def password_present(cls, value: Any) -> str:
        return _password(value)


class ChangePasswordRequest(BaseModel):
    currentPassword: str = Field(min_length=1)
    newPassword: str

    @field_validator("newPassword", mode="before")
    @classmethod
    def password_present(cls, value: Any) -> str:
        return _password(value)
