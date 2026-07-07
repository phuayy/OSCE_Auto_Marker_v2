from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.json_utils import read_json_file
from app.core.security import (
    STREAM_TICKET_SCOPE,
    build_auth_payload,
    build_stream_ticket_payload,
    hash_password,
    new_secret_hex,
    sign_payload,
    verify_password,
    verify_signed_token,
)
from app.core.token_revocation import TokenRevocationRegistry


@dataclass
class RuntimeSecrets:
    auth_secret: str
    nvidia_api_key: str
    whisperx_hf_token: str


class AuthService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._runtime = RuntimeSecrets(auth_secret="", nvidia_api_key="", whisperx_hf_token="")
        self._revocations = TokenRevocationRegistry()

    @property
    def runtime(self) -> RuntimeSecrets:
        return self._runtime

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        paths = self.settings.paths
        paths.auth_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_credentials_file(paths.credentials_path)
        auth_secret = self._ensure_auth_secret(paths.auth_secret_path)
        secrets_payload = self._ensure_secrets_file(paths.secrets_path)
        self._runtime = RuntimeSecrets(
            auth_secret=auth_secret,
            nvidia_api_key=str(os.getenv("NVIDIA_API_KEY") or secrets_payload.get("nvidiaApiKey") or "").strip(),
            whisperx_hf_token=str(
                os.getenv("WHISPERX_HF_TOKEN") or secrets_payload.get("whisperxHfToken") or ""
            ).strip(),
        )

    def _ensure_credentials_file(self, path: Path) -> dict[str, Any]:
        existing = read_json_file(path)
        if existing and isinstance(existing.get("username"), str) and isinstance(existing.get("passwordHash"), str):
            return existing
        if not self.settings.default_admin_password:
            raise RuntimeError(
                "DEFAULT_ADMIN_PASSWORD is required for first boot when "
                "storage/auth/credentials.json is missing. Set it in .env or platform secrets."
            )
        payload = {
            "username": self.settings.default_admin_username,
            "passwordHash": hash_password(self.settings.default_admin_password, self.settings.auth_bcrypt_rounds),
            "createdAt": self._now_iso(),
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return payload

    def _ensure_auth_secret(self, path: Path) -> str:
        env_secret = str(os.getenv("AUTH_SECRET") or "").strip()
        if env_secret:
            return env_secret

        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = new_secret_hex(64)
        path.write_text(value, encoding="utf-8")
        return value

    def _ensure_secrets_file(self, path: Path) -> dict[str, Any]:
        existing = read_json_file(path)
        if existing is not None:
            return existing
        payload = {
            "nvidiaApiKey": "",
            "whisperxHfToken": "",
            "note": (
                "These secrets are server-only. For production, prefer .env or platform secrets. "
                "The FastAPI server reads environment variables first, then this file, and forwards "
                "NVIDIA_API_KEY / WHISPERX_HF_TOKEN env vars."
            ),
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return payload

    async def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        credentials = await asyncio.to_thread(read_json_file, self.settings.paths.credentials_path)
        if not credentials:
            return None
        if str(username or "").strip().lower() != str(credentials.get("username", "")).lower():
            return None
        if not verify_password(password, str(credentials.get("passwordHash", ""))):
            return None

        payload, expires_at = build_auth_payload(
            str(credentials["username"]),
            self.settings.auth_token_ttl_seconds,
        )
        return {
            "token": sign_payload(payload, self._runtime.auth_secret),
            "expiresAt": expires_at,
            "username": str(credentials["username"]),
        }

    def verify_token(self, token: str) -> dict[str, Any] | None:
        """Verify a full session bearer token.

        Stream tickets (``scope == "stream"``) and revoked tokens are rejected so
        a short-lived media ticket can never be used as an API credential.
        """
        if not self._runtime.auth_secret:
            return None
        payload = verify_signed_token(token, self._runtime.auth_secret)
        if not payload:
            return None
        if payload.get("scope") == STREAM_TICKET_SCOPE:
            return None
        if self._revocations.is_revoked(str(payload.get("tokenId") or "")):
            return None
        return payload

    def issue_stream_ticket(self, username: str) -> dict[str, Any]:
        """Mint a short-lived ticket for SSE/media URLs (no Authorization header)."""
        payload, expires_at = build_stream_ticket_payload(username, self.settings.stream_ticket_ttl_seconds)
        return {"ticket": sign_payload(payload, self._runtime.auth_secret), "expiresAt": expires_at}

    def verify_stream_ticket(self, ticket: str) -> dict[str, Any] | None:
        """Verify a media/SSE stream ticket; rejects full tokens and revoked ids."""
        if not self._runtime.auth_secret:
            return None
        payload = verify_signed_token(ticket, self._runtime.auth_secret)
        if not payload or payload.get("scope") != STREAM_TICKET_SCOPE:
            return None
        if self._revocations.is_revoked(str(payload.get("tokenId") or "")):
            return None
        return payload

    def revoke_token(self, token: str) -> bool:
        """Revoke a token (logout). Returns True if a valid token was revoked."""
        if not self._runtime.auth_secret:
            return False
        payload = verify_signed_token(token, self._runtime.auth_secret)
        if not payload:
            return False
        token_id = str(payload.get("tokenId") or "")
        expires_at_ms = int(payload.get("expiresAt") or 0)
        if not token_id:
            return False
        self._revocations.revoke(token_id, expires_at_ms)
        return True

    @staticmethod
    def _now_iso() -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
