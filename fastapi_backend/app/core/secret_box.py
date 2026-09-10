"""Authenticated encryption for operator-entered secrets held in the database.

An API key typed into the settings screen has a longer life and a wider blast
radius than the session it was typed in: it sits in the application database,
which is copied into backups, restored onto laptops, and read by whoever can
open a SQLite file. Storing it verbatim there would mean the database *is* the
credential. So the row holds ciphertext, and the key that opens it lives with
the deployment rather than with the data.

AES-256-GCM, one random 96-bit nonce per write, with the provider id as
additional authenticated data. The AAD matters: without it, an attacker who can
write to the database could move NVIDIA's ciphertext onto the OpenAI row and
have the server send an NVIDIA key to OpenAI. Binding the ciphertext to the
provider it was entered for makes that a decryption failure instead.

The master key is resolved once per process:

* ``CREDENTIAL_ENCRYPTION_KEY`` if set — 32 bytes, base64 or hex. This is the
  production answer: the key comes from the platform's secret store, so a stolen
  database dump is inert on its own.
* otherwise HKDF-SHA256 over the deployment's ``auth_secret`` with a distinct
  ``info`` label. That secret is already a 64-byte random value kept out of the
  database (``AUTH_SECRET`` or ``storage/auth/secret.key``, gitignored), so a
  fresh install gets encryption at rest with no extra setup. The label means the
  derived key is not the token-signing key: leaking one does not hand over the
  other.

Every row records the fingerprint of the key that sealed it. A deployment whose
master key changed reads its rows as *unreadable* and asks the operator to
re-enter, rather than decrypting to garbage or, worse, silently falling back to
some other credential.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Bumped only if the derivation itself changes. Part of the HKDF info string, so
# a change produces a different key and therefore a different fingerprint, which
# surfaces as "re-enter your keys" rather than as corrupt plaintext.
KEY_DERIVATION_INFO = b"osce-ai-marker/provider-credentials/v1"
NONCE_BYTES = 12
KEY_BYTES = 32


class SecretBoxError(RuntimeError):
    """The master key could not be resolved from this deployment."""


class SecretDecryptionError(RuntimeError):
    """A stored secret could not be opened with this deployment's master key."""


def _decode_master_key(raw: str) -> bytes:
    """Accept the two shapes an operator realistically pastes: base64 or hex."""
    text = str(raw or "").strip()
    if not text:
        raise SecretBoxError("CREDENTIAL_ENCRYPTION_KEY is empty.")

    if re.fullmatch(r"[0-9a-fA-F]{64}", text):
        return bytes.fromhex(text)

    padded = text.replace("-", "+").replace("_", "/")
    padded += "=" * ((4 - len(padded) % 4) % 4)
    try:
        decoded = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise SecretBoxError(
            "CREDENTIAL_ENCRYPTION_KEY must be 32 bytes encoded as base64 or 64 hex characters."
        ) from error
    if len(decoded) != KEY_BYTES:
        raise SecretBoxError(
            f"CREDENTIAL_ENCRYPTION_KEY decoded to {len(decoded)} bytes; 32 are required."
        )
    return decoded


def derive_master_key(*, env_key: str = "", auth_secret: str = "") -> bytes:
    """The 32-byte key this deployment seals provider credentials with."""
    if str(env_key or "").strip():
        return _decode_master_key(env_key)
    secret = str(auth_secret or "").strip()
    if not secret:
        raise SecretBoxError(
            "No credential encryption key available: set CREDENTIAL_ENCRYPTION_KEY, or let the "
            "server initialise its auth secret first."
        )
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=None,
        info=KEY_DERIVATION_INFO,
    ).derive(secret.encode("utf-8"))


@dataclass(frozen=True)
class SealedSecret:
    ciphertext: str
    nonce: str
    key_fingerprint: str


class SecretBox:
    """Seals and opens one deployment's secrets."""

    def __init__(self, master_key: bytes) -> None:
        if len(master_key) != KEY_BYTES:
            raise SecretBoxError(f"Master key must be {KEY_BYTES} bytes, got {len(master_key)}.")
        self._aead = AESGCM(master_key)
        # A truncated hash of the key, never the key. Its only job is to tell
        # "sealed by this deployment" from "sealed by a key we no longer have".
        self.fingerprint = hashlib.sha256(b"fingerprint:" + master_key).hexdigest()[:16]

    def seal(self, plaintext: str, *, aad: str) -> SealedSecret:
        nonce = os.urandom(NONCE_BYTES)
        sealed = self._aead.encrypt(nonce, plaintext.encode("utf-8"), aad.encode("utf-8"))
        return SealedSecret(
            ciphertext=base64.b64encode(sealed).decode("ascii"),
            nonce=base64.b64encode(nonce).decode("ascii"),
            key_fingerprint=self.fingerprint,
        )

    def open(self, sealed: SealedSecret, *, aad: str) -> str:
        if sealed.key_fingerprint and sealed.key_fingerprint != self.fingerprint:
            raise SecretDecryptionError(
                "This secret was encrypted with a different key than this deployment now holds."
            )
        try:
            raw = self._aead.decrypt(
                base64.b64decode(sealed.nonce),
                base64.b64decode(sealed.ciphertext),
                aad.encode("utf-8"),
            )
        except (InvalidTag, binascii.Error, ValueError) as error:
            raise SecretDecryptionError("Stored secret failed authentication and cannot be read.") from error
        return raw.decode("utf-8")


def mask_secret(value: str, *, visible: int = 4) -> str:
    """What the settings screen is allowed to see.

    Enough tail to recognise which key is installed when comparing against a
    vendor dashboard, never enough to use. Short values are masked entirely
    rather than half-revealed.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= visible * 2:
        return "•" * len(text)
    return f"{'•' * 6}{text[-visible:]}"


def redact_secrets(text: str, secrets: tuple[str, ...]) -> str:
    """Strip any literal key out of a message before it is returned or logged.

    Providers occasionally echo the credential they were handed back in an error
    body. Anything derived from a provider error passes through here, so a
    connection-test failure cannot become the way a key escapes.
    """
    result = str(text or "")
    for secret in secrets:
        candidate = str(secret or "").strip()
        if len(candidate) >= 8 and candidate in result:
            result = result.replace(candidate, mask_secret(candidate))
    return result
