from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
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


def build_auth_payload(username: str, ttl_seconds: int) -> tuple[dict[str, Any], int]:
    now_ms = int(time.time() * 1000)
    expires_at = now_ms + ttl_seconds * 1000
    return (
        {
            "username": username,
            "issuedAt": now_ms,
            "expiresAt": expires_at,
            "tokenId": str(uuid4()),
        },
        expires_at,
    )


# Scope claim that distinguishes short-lived media/SSE tickets from full
# session bearer tokens, so a ticket can never be used as an API credential.
STREAM_TICKET_SCOPE = "stream"


def build_stream_ticket_payload(username: str, ttl_seconds: int) -> tuple[dict[str, Any], int]:
    now_ms = int(time.time() * 1000)
    expires_at = now_ms + ttl_seconds * 1000
    return (
        {
            "username": username,
            "scope": STREAM_TICKET_SCOPE,
            "issuedAt": now_ms,
            "expiresAt": expires_at,
            "tokenId": str(uuid4()),
        },
        expires_at,
    )
