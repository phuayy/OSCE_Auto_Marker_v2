from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.config import Settings
from app.core.json_utils import read_json_file
from app.core.secure_files import SECRET_DIR_MODE, harden, write_secret_text
from app.core.security import (
    STREAM_TICKET_SCOPE,
    TokenSubject,
    build_auth_payload,
    build_stream_ticket_payload,
    hash_password,
    new_secret_hex,
    sign_payload,
    verify_password,
    verify_signed_token,
)
from app.core.snapshot_cache import SnapshotCache
from app.database.models import utc_now
from app.domain.users import normalize_login_identifier
from app.repositories.token_revocation_repository import TokenRevocationRepository
from app.repositories.user_repository import UserRepository
from app.services.user_directory import UserDirectory, UserSnapshot

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from app.services.change_feed_service import ChangeFeedService

logger = logging.getLogger(__name__)

# Physical table name, as the change trigger reports it.
REVOKED_TOKENS_TABLE = "revoked_tokens"


@dataclass
class RuntimeSecrets:
    auth_secret: str
    nvidia_api_key: str
    whisperx_hf_token: str


class AuthService:
    """Issues and verifies bearer tokens for the accounts in the database.

    Identity used to be one JSON file — a username and a hash — and a token was
    only a signed username. Accounts are rows now (``users``), and a token
    names one (``sub``) together with the ``tokenVersion`` it was issued under.
    Verification is therefore two steps: the signature and expiry, as before,
    and then the account row — still present, still active, still on that
    version — read through :class:`UserDirectory`, which the change feed
    evicts. That second step is what lets an admin's "disable" take effect on
    the marker's next request in every API process, and what makes a password
    change log the account out everywhere.

    This service still owns the deployment's signing secret and the server-only
    secrets file; it no longer owns any credential of its own.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        users: UserRepository,
        directory: UserDirectory,
        changes: "ChangeFeedService | None" = None,
    ) -> None:
        self.settings = settings
        self.users = users
        self.directory = directory
        self._runtime = RuntimeSecrets(auth_secret="", nvidia_api_key="", whisperx_hf_token="")
        # Durable, cross-process revocation: the same SnapshotCache-over-a-
        # change-tracked-table pattern UserDirectory uses over `users`. A
        # revoked token's id is a database fact, not process memory, so a
        # restart or a second process (a Hatchet worker aside, which never
        # verifies a token) honours a logout immediately.
        self._revocation_repository = TokenRevocationRepository(self.users.database)
        self._revocation_cache: SnapshotCache[frozenset[str]] = SnapshotCache(
            "token-revocations",
            token_provider=((lambda: changes.token((REVOKED_TOKENS_TABLE,))) if changes is not None else None),
        )
        if changes is not None:
            changes.add_change_observer(self._revocation_cache.observer_for(REVOKED_TOKENS_TABLE))
        # A real bcrypt hash of a random value, at this deployment's cost
        # factor, verified against whenever a login names an account that does
        # not exist or cannot sign in. Without it a wrong password costs one
        # bcrypt round and an unknown username costs none, and that difference
        # is measurable enough to enumerate accounts by timing. Built in
        # initialize() so it matches the rounds real hashes use.
        self._dummy_hash = ""

    @property
    def runtime(self) -> RuntimeSecrets:
        return self._runtime

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)
        try:
            purged = await self._revocation_repository.purge_expired(utc_now())
            if purged:
                logger.info("Purged %d expired token revocation(s).", purged)
        except Exception:  # noqa: BLE001 - housekeeping must never block a boot
            logger.warning("Could not purge expired token revocations.", exc_info=True)

    def _initialize_sync(self) -> None:
        paths = self.settings.paths
        paths.auth_dir.mkdir(parents=True, exist_ok=True)
        # A directory created before this hardening existed is narrowed the
        # same way an existing secret file is, below.
        harden(paths.auth_dir, mode=SECRET_DIR_MODE)
        self._dummy_hash = hash_password(new_secret_hex(16), self.settings.auth_bcrypt_rounds)
        auth_secret = self._ensure_auth_secret(paths.auth_secret_path)
        secrets_payload = self._ensure_secrets_file(paths.secrets_path)
        self._runtime = RuntimeSecrets(
            auth_secret=auth_secret,
            nvidia_api_key=str(os.getenv("NVIDIA_API_KEY") or secrets_payload.get("nvidiaApiKey") or "").strip(),
            whisperx_hf_token=str(
                os.getenv("WHISPERX_HF_TOKEN") or secrets_payload.get("whisperxHfToken") or ""
            ).strip(),
        )

    def _ensure_auth_secret(self, path: Path) -> str:
        env_secret = str(os.getenv("AUTH_SECRET") or "").strip()
        if env_secret:
            return env_secret

        if path.exists():
            # Narrowed here too: a file from before this module existed is
            # exactly as exposed as a brand new one until something does this.
            harden(path)
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = new_secret_hex(64)
        write_secret_text(path, value)
        return value

    def _ensure_secrets_file(self, path: Path) -> dict[str, Any]:
        existing = read_json_file(path)
        if existing is not None:
            harden(path)
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
        write_secret_text(path, json.dumps(payload, indent=2) + "\n")
        return payload

    # --- login ---------------------------------------------------------------

    async def authenticate(self, identifier: str, password: str) -> dict[str, Any] | None:
        """A bearer token for the account ``identifier`` names, or None.

        Every refusal — unknown account, invited but not yet activated,
        disabled, wrong password — costs one bcrypt verification and answers
        identically, so the login endpoint cannot be used to learn which
        addresses have accounts.
        """
        record = await self.users.find_by_login(normalize_login_identifier(identifier))
        snapshot = await self.directory.get(record.id) if record is not None else None
        stored_hash = record.password_hash if record is not None else None
        usable = snapshot is not None and snapshot.active and bool(stored_hash)

        verified = await asyncio.to_thread(verify_password, password, stored_hash if usable else self._dummy_hash)
        if not usable or not verified:
            return None

        assert record is not None and snapshot is not None  # for the type checker
        await self.users.record_login(record.id)
        # record_login writes the row, which moves the table's counter; the
        # snapshot used below predates that write but nothing in it changed.
        return self.issue_session_token(snapshot)

    def issue_session_token(self, subject: UserSnapshot) -> dict[str, Any]:
        """Mint a bearer token for an account already known to be usable —
        after a login, or after a password change that must keep this tab
        signed in while every other session is revoked."""
        payload, expires_at = build_auth_payload(self._subject(subject), self.settings.auth_token_ttl_seconds)
        return {
            "token": sign_payload(payload, self._runtime.auth_secret),
            "expiresAt": expires_at,
            **self.describe_subject(subject),
        }

    @staticmethod
    def describe_subject(subject: UserSnapshot) -> dict[str, Any]:
        """The identity fields a client may hold: never the hash, never the version."""
        return {
            "userId": subject.id,
            "username": subject.username,
            "role": subject.role.value,
            "displayName": subject.display_name,
            "email": subject.email,
        }

    @staticmethod
    def _subject(snapshot: UserSnapshot) -> TokenSubject:
        return TokenSubject(
            user_id=snapshot.id,
            username=snapshot.username,
            role=snapshot.role.value,
            token_version=snapshot.token_version,
        )

    # --- verification --------------------------------------------------------

    async def verify_token(self, token: str) -> dict[str, Any] | None:
        """Verify a full session bearer token.

        Stream tickets (``scope == "stream"``) and revoked tokens are rejected so
        a short-lived media ticket can never be used as an API credential. The
        payload returned carries the account's *current* role, read from the
        row, so a role change applies without a new login.
        """
        payload = await self._verify_signature(token)
        if not payload or payload.get("scope") == STREAM_TICKET_SCOPE:
            return None
        return await self._bind_to_account(payload)

    async def verify_stream_ticket(self, ticket: str) -> dict[str, Any] | None:
        """Verify a media/SSE stream ticket; rejects full tokens and revoked ids."""
        payload = await self._verify_signature(ticket)
        if not payload or payload.get("scope") != STREAM_TICKET_SCOPE:
            return None
        return await self._bind_to_account(payload)

    async def _verify_signature(self, token: str) -> dict[str, Any] | None:
        if not self._runtime.auth_secret:
            return None
        payload = verify_signed_token(token, self._runtime.auth_secret)
        if not payload:
            return None
        revoked_ids = await self._revocation_cache.get(self._load_revoked_ids)
        if str(payload.get("tokenId") or "") in revoked_ids:
            return None
        return payload

    async def _load_revoked_ids(self) -> frozenset[str]:
        return frozenset(await self._revocation_repository.list_active_ids(utc_now()))

    async def _bind_to_account(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """The second half of verification: the account behind the token.

        A token with no ``sub`` predates accounts-as-rows and is refused —
        everyone signs in once after the upgrade. A token whose version no
        longer matches the row was issued before a password change, a disable
        or a role change bumped it, and is refused the same way.
        """
        user_id = str(payload.get("sub") or "")
        if not user_id:
            return None
        snapshot = await self.directory.get(user_id)
        if snapshot is None or not snapshot.active:
            return None
        if int(payload.get("tokenVersion") or 0) != snapshot.token_version:
            return None
        return {
            **payload,
            "username": snapshot.username,
            "role": snapshot.role.value,
            "displayName": snapshot.display_name,
            "email": snapshot.email,
        }

    # --- stream tickets and logout -------------------------------------------

    async def issue_stream_ticket(self, user_id: str) -> dict[str, Any] | None:
        """Mint a short-lived ticket for SSE/media URLs (no Authorization header)."""
        snapshot = await self.directory.get(user_id)
        if snapshot is None or not snapshot.active:
            return None
        payload, expires_at = build_stream_ticket_payload(
            self._subject(snapshot), self.settings.stream_ticket_ttl_seconds
        )
        return {"ticket": sign_payload(payload, self._runtime.auth_secret), "expiresAt": expires_at}

    async def revoke_token(self, token: str) -> bool:
        """Revoke a token (logout). Returns True if a valid token was revoked.

        Written to the database, not held in process memory — a restart or a
        second API process must honour this logout too, which a purely local
        registry cannot. The cache is invalidated directly (the "a local
        write evicts directly" rule every other cache in this codebase
        follows) so this same process's very next verification already sees
        it, without waiting for the change-feed round trip.
        """
        if not self._runtime.auth_secret:
            return False
        payload = verify_signed_token(token, self._runtime.auth_secret)
        if not payload:
            return False
        token_id = str(payload.get("tokenId") or "")
        expires_at_ms = int(payload.get("expiresAt") or 0)
        if not token_id:
            return False
        user_id = str(payload.get("sub") or "") or None
        expires_at = datetime.fromtimestamp(expires_at_ms / 1000, tz=timezone.utc)
        await self._revocation_repository.revoke(token_id, user_id, expires_at)
        self._revocation_cache.invalidate()
        return True

    def cache_stats(self) -> dict[str, Any]:
        return self._revocation_cache.stats()
