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

**Caching.** Decrypting every key on every scoring call is wasted work: the table
changes a handful of times a year and is read on every run, in every process. So
one read produces a :class:`CredentialSnapshot` — decrypted keys *and* the masked
metadata the settings screen renders — which is then reused. Freshness rests on
the same two mechanisms :class:`~app.core.versioned_cache.VersionedCache` uses,
because a cached credential that outlives its rotation is worse than no cache:

* **Push** — ``provider_credentials`` is a change-tracked table, so a write fires
  a database trigger that bumps ``table_versions`` and (on PostgreSQL) issues a
  ``pg_notify``. ``ChangeFeedService`` turns that into an observer callback in
  *every* process, so a key rotated in the API evicts the Hatchet worker's copy
  within milliseconds without either process polling.
* **Token comparison** — the snapshot records the ``table_versions`` counter it
  was built from, and a hit requires that counter to still match. This covers
  what push cannot: the window while the listener reconnects, a SQLite
  deployment with no ``NOTIFY``, an event dropped under back-pressure.

Local writes evict directly rather than waiting for the announcement to come
back round. That is not an optimisation but a correctness requirement: with the
listener connected the token is answered from memory, so the counter this process
holds does not move until its own notification arrives, and the HTTP response to
a rotation would otherwise be built from the pre-rotation snapshot.

A service built with no change feed does **not** cache at all. Degrading to the
old per-call read is slower; degrading to a cache with no way to learn about a
rotation would keep sending a revoked key, and that trade only has one answer.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.core.secret_box import (
    SealedSecret,
    SecretBox,
    SecretBoxError,
    SecretDecryptionError,
    derive_master_key,
    mask_secret,
)
from app.core.snapshot_cache import SnapshotCache
from app.repositories.provider_credential_repository import ProviderCredentialRepository

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from app.services.change_feed_service import ChangeFeedService

logger = logging.getLogger(__name__)

# The table whose counter this cache watches. Must match the name the trigger
# reports, which is the physical table name.
CREDENTIALS_TABLE = "provider_credentials"

# Long enough that a truncated paste is caught here rather than by a 401 forty
# minutes into a run; short enough to accept every vendor's format.
MIN_KEY_LENGTH = 8
MAX_KEY_LENGTH = 512

# Shown for a row that has no recorded tail (written before `last4` existed).
MASK_PLACEHOLDER = "•" * 6

# The mask this app shows and the placeholders a browser autofills. Saving one
# of these would replace a working key with a string of dots.
_MASK_CHARACTERS = re.compile(r"^[•\*….\s]+$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class CredentialError(ValueError):
    """The submitted key is not storable. Message is safe to show the operator."""


@dataclass(frozen=True)
class CredentialSnapshot:
    """One decrypted read of the whole credential table.

    Both consumers are served from the same object: ``api_keys`` is what the
    router authenticates with, ``statuses`` is the masked metadata the settings
    screen renders. Building them together means one query and one decrypt pass
    per change, instead of one per caller.

    Freshness is not this object's concern — :class:`SnapshotCache` holds it
    against the ``table_versions`` counter it was built from.
    """

    api_keys: Mapping[str, str] = field(default_factory=dict)
    statuses: Mapping[str, dict[str, Any]] = field(default_factory=dict)


class ProviderCredentialService:
    def __init__(
        self,
        repository: ProviderCredentialRepository,
        *,
        master_key_source: Callable[[], str] | None = None,
        env_key: str = "",
        changes: "ChangeFeedService | None" = None,
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

        # --- cache -----------------------------------------------------------
        # Without a change feed nothing can announce a rotation, so the cache
        # disables itself and every read goes to the database — slower, and the
        # only safe way to be wrong about a credential.
        self._cache: SnapshotCache[CredentialSnapshot] = SnapshotCache(
            "provider-credentials",
            token_provider=(
                (lambda: changes.token((CREDENTIALS_TABLE,))) if changes is not None else None
            ),
        )
        if changes is not None:
            # How a rotation in another process reaches this one. Registered at
            # construction because the observer list is only read after start().
            changes.add_change_observer(self._cache.observer_for(CREDENTIALS_TABLE))

    # --- cache -------------------------------------------------------------

    def invalidate(self, reason: str = "") -> None:
        """Drop the cached snapshot. Safe to call from anywhere, including sync
        contexts and the change-feed handler."""
        self._cache.invalidate(reason)

    async def snapshot(self) -> CredentialSnapshot:
        """The decrypted credential table, from cache when it is still current."""
        return await self._cache.get(self._build)

    async def _build(self) -> CredentialSnapshot:
        """One query, one decrypt pass, both projections."""
        box = self._resolve_box()
        api_keys: dict[str, str] = {}
        statuses: dict[str, dict[str, Any]] = {}
        for record in await self.repository.list_all():
            status = ProviderCredentialRepository.to_status(record)
            readable = box is not None and (
                not record.key_fingerprint or record.key_fingerprint == box.fingerprint
            )
            if readable and box is not None:
                try:
                    api_keys[record.provider_id] = box.open(
                        SealedSecret(record.ciphertext, record.nonce, record.key_fingerprint),
                        aad=record.provider_id,
                    )
                except SecretDecryptionError as error:
                    # One unreadable row must not stop the others from scoring.
                    readable = False
                    logger.warning(
                        "Stored API key for provider '%s' could not be decrypted: %s",
                        record.provider_id,
                        error,
                    )
            status["readable"] = readable
            status["maskedKey"] = (
                mask_secret(f"{'x' * 12}{record.last4}") if record.last4 else MASK_PLACEHOLDER
            )
            statuses[record.provider_id] = status
        return CredentialSnapshot(api_keys=api_keys, statuses=statuses)

    def cache_stats(self) -> dict[str, Any]:
        """Hit/miss/eviction counters, for the readiness payload."""
        return self._cache.stats()

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

        Prefer :meth:`snapshot` when the caller also wants ``statuses`` — this is
        the convenience wrapper, and calling both re-enters the cache twice.
        """
        return dict((await self.snapshot()).api_keys)

    async def statuses(self) -> dict[str, dict[str, Any]]:
        """Masked, non-secret metadata per provider, for the settings screen."""
        return {
            provider_id: dict(status)
            for provider_id, status in (await self.snapshot()).statuses.items()
        }

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
        # Evicted here, not left to the trigger's announcement. With the listener
        # connected the token is served from memory, so this process's counter
        # does not move until its own notification arrives — and the response to
        # this very request re-reads the snapshot. Waiting would answer a
        # rotation with the key it just replaced.
        self.invalidate(f"key stored for '{provider_id}'")
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
            # Revocation is the case where a stale cache does real damage, so it
            # is dropped unconditionally rather than on the announcement.
            self.invalidate(f"key removed for '{provider_id}'")
            logger.info(
                "Removed the stored API key for LLM provider '%s' (by %s); the environment "
                "value, if any, applies again.",
                provider_id,
                actor or "unknown",
            )
        return removed

    async def record_test(self, provider_id: str, *, ok: bool, error: str = "") -> None:
        await self.repository.record_test(provider_id, ok=ok, error=error)
        # The verdict lives in the snapshot's `statuses`, which the settings
        # screen renders immediately after a test.
        self.invalidate(f"test result recorded for '{provider_id}'")
