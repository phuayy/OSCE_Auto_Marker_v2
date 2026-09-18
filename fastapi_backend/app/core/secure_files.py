"""Writing and hardening files a deployment's identity depends on.

``storage/auth/secret.key`` signs every bearer token this deployment issues,
and ``AES-256-GCM`` provider-key encryption derives its master key from it
when no ``CREDENTIAL_ENCRYPTION_KEY`` is set (see ``core/secret_box.py``); the
platform secrets file next to it can carry plaintext vendor API keys. Written
with ``Path.write_text`` alone, both land at whatever the process umask
allows — 0644 on an ordinary Linux host, world-readable. On a shared VM that
hands every local account this deployment's signing key (forge any bearer
token, including an admin's) and, through it, the credential-encryption key.

Two entry points: :func:`write_secret_text` for a file this process is
creating fresh, and :func:`harden` for a file or directory that may already
exist from before this module did — an upgraded deployment's existing
`secret.key` is exactly as exposed as a brand new one until something narrows
it once.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

# Owner read/write only — nothing for group or other. What every file this
# module writes needs: the HMAC signing key and the platform secrets file.
SECRET_FILE_MODE = 0o600
# Owner read/write/execute only, for the directory that holds them.
SECRET_DIR_MODE = 0o700


def write_secret_text(path: Path, text: str) -> None:
    """Create ``path`` with ``text``, readable only by its owner from the
    first byte on.

    POSIX: ``os.open`` with an explicit mode sets the permission bits at
    creation, atomically — a separate ``chmod()`` after ``write_text`` would
    leave a window where the file exists at the umask's default (typically
    world-readable) before anything narrows it. Windows has no equivalent
    concept (NTFS ACLs are a different model, and this deployment's supported
    target is Linux), so there ``os.open``'s ``mode`` argument is ignored and
    this is a plain write — harmless, since a Windows host is a development
    machine here, not a shared multi-tenant one.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SECRET_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def harden(path: Path, *, mode: int = SECRET_FILE_MODE) -> None:
    """Narrow ``path``'s permissions to ``mode`` if they are broader than that.

    Idempotent and safe on every startup: a path already at ``mode`` (or
    narrower) is left untouched, and any failure — Windows, a filesystem with
    no POSIX permission bits, a permission error narrowing someone else's
    file — is logged and swallowed. A deployment this cannot be verified on
    must still start; it is a hardening pass, not a precondition.
    """
    if os.name != "posix":
        return
    try:
        current = stat.S_IMODE(path.stat().st_mode)
        if current & ~mode:
            path.chmod(mode)
    except OSError:
        logger.warning("Could not verify or narrow permissions on %s.", path, exc_info=True)
