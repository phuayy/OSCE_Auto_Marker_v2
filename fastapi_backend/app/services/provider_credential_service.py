"""Provider API keys an operator can set, rotate and revoke from the settings screen.

Why this exists at all: the deployment's ``.env`` is the wrong place to rotate a
credential from. Editing it means shell access to the server and a restart, so in
practice keys are rotated late or never, and a leaked key stays live because
replacing it is a deploy. Moving the key into the application — write-only,
encrypted, applied to the very next run — makes rotation a thirty-second action,
which is the single biggest thing that reduces the damage a leak can do.

The trade that buys is "the database now holds credentials", and everything in
this module exists to pay it down:

* **Encrypted at rest.** The row is AES-256-GCM ciphertext bound to its provider
  id; the key that opens it comes from ``CREDENTIAL_ENCRYPTION_KEY`` or is
  derived from the deployment's auth secret. A stolen database file is not a
  stolen credential (see ``app/core/secret_box.py``).
* **Write-only over the API.** No endpoint returns a key. Reads answer with the
  last four characters, who set it and when, and the last test result.
* **Never in ``app_settings``.** That table is dumped verbatim to every settings
  reader; this one is never serialised.
* **Narrow forwarding.** A key reaches a subprocess only when the routing
  actually names its provider, via ``credential_env_for``.
* **Redacted on the way out.** Provider error text passes through
  ``redact_secrets`` before it reaches a response or a log, because vendors do
  sometimes echo the credential they rejected.

Precedence is: a key saved here wins over one in the environment. That is the
point of the feature — an operator rotating a key at 2am must not be overruled
by a stale ``.env`` — and the settings screen labels which source is in force so
the override is never a surprise.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from app.core.secret_box import (
    SealedSecret,
    SecretBox,
    SecretBoxError,
    SecretDecryptionError,
    derive_master_key,
    mask_secret,
)
from app.repositories.provider_credential_repository import ProviderCredentialRepository

logger = logging.getLogger(__name__)

# Long enough that a truncated paste is caught here rather than by a 401 forty
# minutes into a run; short enough to accept every vendor's format.
MIN_KEY_LENGTH = 8
MAX_KEY_LENGTH = 512

# The mask this app shows and the placeholders a browser autofills. Saving one
# of these would replace a working key with a string of dots.
_MASK_CHARACTERS = re.compile(r"^[•\*….\s]+$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class CredentialError(ValueError):
    """The submitted key is not storable. Message is safe to show the operator."""


class ProviderCredentialService:
    def __init__(
        self,
        repository: ProviderCredentialRepository,
        *,
        master_key_source: Callable[[], str] | None = None,
        env_key: str = "",
    ) -> None:
        self.repository = repository
        # Resolved through a callable rather than captured now: the container is
        # built before AuthService initialises, so a snapshot taken here would
        # always be the empty string and every deployment would fall back to
        # "no encryption key available".
        self._master_key_source = master_key_source
        self._env_key = str(env_key or "").strip()
        self._box: SecretBox | None = None
        self._box_error = ""

    # --- encryption --------------------------------------------------------

    def _resolve_box(self) -> SecretBox | None:
        if self._box is not None:
            return self._box
        try:
            auth_secret = self._master_key_source() if self._master_key_source else ""
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed to read the deployment secret used to encrypt provider keys.")
            auth_secret = ""
        try:
            self._box = SecretBox(derive_master_key(env_key=self._env_key, auth_secret=auth_secret))
            self._box_error = ""
        except SecretBoxError as error:
            # Not cached: the auth secret arrives during startup, so a failure
            # before that must not permanently disable the feature.
            self._box_error = str(error)
            return None
        return self._box

    def encryption_status(self) -> dict[str, Any]:
        """Whether keys can be stored here at all, for the settings screen."""
        box = self._resolve_box()
        return {
            "available": box is not None,
            "source": "environment" if self._env_key else "derived",
            "keyFingerprint": box.fingerprint if box else "",
            "reason": self._box_error,
        }

    # --- validation --------------------------------------------------------

    @staticmethod
    def normalize_key(raw: str) -> str:
        key = str(raw or "").strip()
        if not key:
            raise CredentialError("Enter an API key.")
        if _MASK_CHARACTERS.fullmatch(key):
            raise CredentialError(
                "That looks like the masked preview rather than a key. Paste the real value."
            )
        if _CONTROL_CHARACTERS.search(key) or any(character.isspace() for character in key):
            raise CredentialError("An API key cannot contain spaces or line breaks.")
        if len(key) < MIN_KEY_LENGTH:
            raise CredentialError(f"That key is only {len(key)} characters - it looks truncated.")
        if len(key) > MAX_KEY_LENGTH:
            raise CredentialError(f"An API key cannot be longer than {MAX_KEY_LENGTH} characters.")
        return key

    # --- reads -------------------------------------------------------------

    async def api_keys(self) -> dict[str, str]:
        """Every readable stored key, by provider id.

        A row this deployment cannot open is skipped with a warning rather than
        raised: one provider sealed under a rotated master key must not stop the
        others from scoring.
        """
        box = self._resolve_box()
        if box is None:
            return {}
        keys: dict[str, str] = {}
        for record in await self.repository.list_all():
            try:
                keys[record.provider_id] = box.open(
                    SealedSecret(record.ciphertext, record.nonce, record.key_fingerprint),
                    aad=record.provider_id,
                )
            except SecretDecryptionError as error:
                logger.warning(
                    "Stored API key for provider '%s' could not be decrypted: %s",
                    record.provider_id,
                    error,
                )
        return keys

    async def statuses(self) -> dict[str, dict[str, Any]]:
        """Masked, non-secret metadata per provider, for the settings screen."""
        box = self._resolve_box()
        result: dict[str, dict[str, Any]] = {}
        for record in await self.repository.list_all():
            status = ProviderCredentialRepository.to_status(record)
            status["readable"] = bool(
                box is not None
                and (not record.key_fingerprint or record.key_fingerprint == box.fingerprint)
            )
            status["maskedKey"] = mask_secret(f"{'x' * 12}{record.last4}") if record.last4 else "•" * 6
            result[record.provider_id] = status
        return result

    # --- writes ------------------------------------------------------------

    async def set_key(self, provider_id: str, api_key: str, *, actor: str = "") -> dict[str, Any]:
        key = self.normalize_key(api_key)
        box = self._resolve_box()
        if box is None:
            raise CredentialError(
                self._box_error
                or "This server has no credential encryption key, so API keys cannot be stored here."
            )
        sealed = box.seal(key, aad=str(provider_id))
        await self.repository.upsert(
            provider_id,
            ciphertext=sealed.ciphertext,
            nonce=sealed.nonce,
            key_fingerprint=sealed.key_fingerprint,
            last4=key[-4:],
            updated_by=str(actor or "") or None,
        )
        # Logged so a rotation is auditable; the value never appears, only its
        # tail, which is the same thing the screen shows.
        logger.info(
            "Stored API key for LLM provider '%s' (%s) set by %s.",
            provider_id,
            mask_secret(key),
            actor or "unknown",
        )
        return {"providerId": str(provider_id), "last4": key[-4:]}

    async def clear_key(self, provider_id: str, *, actor: str = "") -> bool:
        removed = await self.repository.delete(provider_id)
        if removed:
            logger.info(
                "Removed the stored API key for LLM provider '%s' (by %s); the environment "
                "value, if any, applies again.",
                provider_id,
                actor or "unknown",
            )
        return removed

    async def record_test(self, provider_id: str, *, ok: bool, error: str = "") -> None:
        await self.repository.record_test(provider_id, ok=ok, error=error)
