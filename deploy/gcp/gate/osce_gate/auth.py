"""Who may switch the GPU machine on.

The gate cannot use the app's own accounts: they live in the database on the
VM, which is off. It uses Google sign-in instead and an allowlist the
operator sets, and fails closed — with no allowlist nobody may start it.
Starting a GPU machine costs money, so an open "Start" button on a public URL
is a bill anyone on the internet could run up.

After Google sign-in the gate keeps a small signed cookie (email + expiry,
HMAC-SHA256), so the page does not ask Google again on every poll.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

COOKIE_NAME = "osce_gate"


@dataclass(frozen=True)
class Allowlist:
    emails: frozenset[str]
    domains: frozenset[str]

    @classmethod
    def parse(cls, emails: str, domains: str) -> "Allowlist":
        def split(raw: str) -> frozenset[str]:
            return frozenset(item.strip().lower().lstrip("@") for item in raw.split(",") if item.strip())

        return cls(emails=split(emails), domains=split(domains))

    def allows(self, email: str) -> bool:
        email = email.strip().lower()
        if not email or "@" not in email:
            return False
        return email in self.emails or email.rsplit("@", 1)[1] in self.domains


class SignInError(Exception):
    pass


def google_token_verifier(client_id: str) -> Callable[[str], dict[str, Any]]:
    """Verify a Google Identity Services ID token for this client id."""

    def verify(credential: str) -> dict[str, Any]:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        return id_token.verify_oauth2_token(credential, google_requests.Request(), client_id)

    return verify


def email_from_claims(claims: dict[str, Any], allowlist: Allowlist) -> str:
    email = str(claims.get("email") or "").lower()
    if not email or not claims.get("email_verified"):
        raise SignInError("Google did not confirm this account's email address.")
    if not allowlist.allows(email):
        raise SignInError(f"{email} is not allowed to start the marker. Ask the administrator to add it.")
    return email


class SessionCookie:
    def __init__(self, secret: str, ttl_seconds: int = 12 * 3600, clock: Callable[[], float] = time.time) -> None:
        if len(secret) < 32:
            raise ValueError("GATE_SESSION_SECRET must be at least 32 characters.")
        self._key = secret.encode()
        self.ttl_seconds = ttl_seconds
        self._clock = clock

    def _sign(self, payload: bytes) -> str:
        return base64.urlsafe_b64encode(hmac.new(self._key, payload, hashlib.sha256).digest()).decode().rstrip("=")

    def issue(self, email: str) -> str:
        payload = f"{email}|{int(self._clock()) + self.ttl_seconds}".encode()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=") + "." + self._sign(payload)

    def read(self, value: str | None, allowlist: Allowlist) -> str | None:
        """The signed-in email, or None. Re-checks the allowlist, so removing
        someone from it takes effect without waiting for their cookie to expire."""
        if not value or "." not in value:
            return None
        encoded, signature = value.rsplit(".", 1)
        try:
            payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except (ValueError, TypeError):
            return None
        if not hmac.compare_digest(self._sign(payload), signature):
            return None
        try:
            email, expires = payload.decode().rsplit("|", 1)
            if int(expires) < self._clock():
                return None
        except ValueError:
            return None
        return email if allowlist.allows(email) else None
