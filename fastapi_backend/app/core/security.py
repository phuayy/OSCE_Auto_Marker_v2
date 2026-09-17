from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import bcrypt


def base64_url_encode(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def base64_url_decode(text: str) -> bytes:
    normalized = str(text or "").replace("-", "+").replace("_", "/")
    padded = normalized + "=" * ((4 - len(normalized) % 4) % 4)
    return base64.b64decode(padded)


def hash_password(password: str, rounds: int = 12) -> str:
    salt = bcrypt.gensalt(rounds=rounds)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def new_secret_hex(byte_count: int = 64) -> str:
    return secrets.token_hex(byte_count)


def sign_payload(payload: dict[str, Any], secret_key: str) -> str:
    payload_json = json.dumps(payload, separators=(",", ":"))
    payload_b64 = base64_url_encode(payload_json)
    signature = hmac.new(secret_key.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    return f"{payload_b64}.{base64_url_encode(signature)}"


def verify_signed_token(token: str, secret_key: str) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    payload_b64, signature_b64 = token.split(".", 1)
    if not payload_b64 or not signature_b64:
        return None

    expected = hmac.new(secret_key.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    try:
        provided = base64_url_decode(signature_b64)
    except Exception:
        return None
    if not hmac.compare_digest(expected, provided):
        return None

    try:
        payload = json.loads(base64_url_decode(payload_b64).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None

    expires_at = float(payload.get("expiresAt") or 0)
    if expires_at < time.time() * 1000:
        return None
    return payload


@dataclass(frozen=True)
class TokenSubject:
    """Who a bearer token or stream ticket speaks for.

    ``token_version`` is copied from the account row at issue time and
    compared with the row on every verification; bumping the row's version is
    how the account's tokens are revoked without a shared revocation store.
    ``role`` is carried for callers that only hold the token, but it is
    informative — verification refreshes it from the row.
    """

    user_id: str
    username: str
    role: str
    token_version: int


def _base_claims(subject: TokenSubject, ttl_seconds: int) -> tuple[dict[str, Any], int]:
    now_ms = int(time.time() * 1000)
    expires_at = now_ms + ttl_seconds * 1000
    return (
        {
            "sub": subject.user_id,
            "username": subject.username,
            "role": subject.role,
            "tokenVersion": int(subject.token_version),
            "issuedAt": now_ms,
            "expiresAt": expires_at,
            "tokenId": str(uuid4()),
        },
        expires_at,
    )


def build_auth_payload(subject: TokenSubject, ttl_seconds: int) -> tuple[dict[str, Any], int]:
    return _base_claims(subject, ttl_seconds)


# Scope claim that distinguishes short-lived media/SSE tickets from full
# session bearer tokens, so a ticket can never be used as an API credential.
STREAM_TICKET_SCOPE = "stream"


def build_stream_ticket_payload(subject: TokenSubject, ttl_seconds: int) -> tuple[dict[str, Any], int]:
    payload, expires_at = _base_claims(subject, ttl_seconds)
    payload["scope"] = STREAM_TICKET_SCOPE
    return payload, expires_at


# --- emailed action tokens ---------------------------------------------------
#
# An invitation or password-reset link carries a capability, not a session: it
# is single-use, short-lived, and stored only as a hash. 32 random bytes is 256
# bits of entropy, which is why a plain SHA-256 (rather than a slow password
# hash) is the right thing to store — there is nothing an attacker could
# brute-force offline in the lifetime of the token.

ACTION_TOKEN_BYTES = 32


def new_action_token() -> str:
    return secrets.token_urlsafe(ACTION_TOKEN_BYTES)


def hash_action_token(raw_token: str) -> str:
    return hashlib.sha256(str(raw_token or "").encode("utf-8")).hexdigest()
