"""Vocabulary for user accounts.

Roles, account statuses and the purposes an emailed action token can have —
plus the normalisation and password rules every layer has to agree on. The
API boundary, the repository and the admin service all import from here, so a
username is lowercased the same way on write and on lookup, and a password is
judged by one policy whether it arrives through an invitation, a reset or a
change-my-password form.

Two roles only, by design: ``admin`` is ``marker`` plus user management and
deployment configuration. Every marker still *sees* every session — there is
no read-side permission matrix — but a session may only be *changed* by its
creator or an admin; see ``app.domain.access``.
"""

from __future__ import annotations

import re
from enum import StrEnum


class UserRole(StrEnum):
    ADMIN = "admin"
    MARKER = "marker"


class UserStatus(StrEnum):
    # Invited by an admin, has not yet followed the emailed link and set a
    # password. Has no credential and cannot log in.
    INVITED = "invited"
    ACTIVE = "active"
    # Kept, but refused at login and on every request. The row stays so an
    # audit trail (who invited whom, who last logged in) survives a suspension.
    DISABLED = "disabled"


class ActionTokenPurpose(StrEnum):
    INVITE = "invite"
    PASSWORD_RESET = "password_reset"


# Length over complexity rules (NIST SP 800-63B): a minimum that rules out the
# trivially guessable, and no character-class theatre.
PASSWORD_MIN_LENGTH = 10
# bcrypt hashes at most 72 bytes and silently ignores the rest, so a longer
# password would be accepted and then verify against its own prefix. Refuse it
# instead of pretending the extra characters count.
PASSWORD_MAX_BYTES = 72

USERNAME_MAX_LENGTH = 120
EMAIL_MAX_LENGTH = 320
DISPLAY_NAME_MAX_LENGTH = 120

# Deliberately loose: the address is proven by the round trip through the
# mailbox, not by a regex. This only rejects what can never be delivered.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._@+-]*$")


class UserVocabularyError(ValueError):
    """A value that cannot be an email, username, role or status."""


def parse_role(raw: object, *, default: UserRole | None = None) -> UserRole:
    text = str(raw or "").strip().lower()
    if not text and default is not None:
        return default
    try:
        return UserRole(text)
    except ValueError:
        known = ", ".join(role.value for role in UserRole)
        raise UserVocabularyError(f"Unknown role '{text}'. Expected one of: {known}.") from None


def parse_status(raw: object) -> UserStatus:
    text = str(raw or "").strip().lower()
    try:
        return UserStatus(text)
    except ValueError:
        known = ", ".join(status.value for status in UserStatus)
        raise UserVocabularyError(f"Unknown user status '{text}'. Expected one of: {known}.") from None


def normalize_email(raw: object) -> str:
    """Lowercased, trimmed, and shaped like an address — or an error."""
    text = str(raw or "").strip().lower()
    if not text:
        raise UserVocabularyError("An email address is required.")
    if len(text) > EMAIL_MAX_LENGTH or not _EMAIL_RE.match(text):
        raise UserVocabularyError(f"'{text}' is not a valid email address.")
    return text


def normalize_username(raw: object) -> str:
    """Lowercased login handle. Markers use their email address as one."""
    text = str(raw or "").strip().lower()
    if not text:
        raise UserVocabularyError("A username is required.")
    if len(text) > USERNAME_MAX_LENGTH or not _USERNAME_RE.match(text):
        raise UserVocabularyError(
            "A username may contain letters, digits and . _ @ + - and must start with a letter or digit."
        )
    return text


def normalize_login_identifier(raw: object) -> str:
    """What the login form sends: a username *or* an email, compared lowercase.

    Never raises — a malformed identifier simply matches no account, and the
    login endpoint must answer that the same way it answers a wrong password.
    """
    return str(raw or "").strip().lower()[:EMAIL_MAX_LENGTH]


def normalize_display_name(raw: object) -> str:
    text = " ".join(str(raw or "").split())
    return text[:DISPLAY_NAME_MAX_LENGTH]


def password_policy_errors(password: str, *, forbidden: tuple[str, ...] = ()) -> list[str]:
    """Every reason a password is refused, so a form can show them all at once.

    ``forbidden`` carries the account's own identifiers (username, email, the
    local part of the email): a password equal to any of them is the one
    "complexity" rule worth keeping, because it is the first thing anyone
    guesses.
    """
    text = str(password or "")
    errors: list[str] = []
    if len(text) < PASSWORD_MIN_LENGTH:
        errors.append(f"Use at least {PASSWORD_MIN_LENGTH} characters.")
    if len(text.encode("utf-8")) > PASSWORD_MAX_BYTES:
        errors.append(f"Use at most {PASSWORD_MAX_BYTES} bytes.")
    lowered = text.strip().lower()
    for candidate in forbidden:
        value = str(candidate or "").strip().lower()
        if value and lowered == value:
            errors.append("The password must not be your username or email address.")
            break
    return errors
