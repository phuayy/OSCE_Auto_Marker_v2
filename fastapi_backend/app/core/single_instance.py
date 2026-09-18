"""One API process per storage root.

Token revocation (``core/token_revocation.py``), the login and token-endpoint
rate limiters (``core/rate_limit.py``) and the per-upload part lock
(``AsyncUploadService``'s ``KeyedLocks``) are all in-process state — nothing
shares them across two API instances pointed at the same storage. A second
instance would not honour a token the first just revoked, would give an
attacker double the login attempts before either limiter notices, and could
interleave two chunks of the same upload past the lock that exists precisely
to serialise them (see CLAUDE.md "Parts are serialised per upload").

A Hatchet worker is a different, and unaffected, process by design: it never
verifies a token, serves no login, and never writes an upload part — the
`role` distinction in `AppContainer.startup()` already keeps its startup
sequence separate, and this lock is acquired only for the API role. Running
several Hatchet workers against one storage root is the supported way to
scale job execution.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import IO

logger = logging.getLogger(__name__)


class SingleInstanceError(RuntimeError):
    """Another process already holds the API lock for this storage root."""


class SingleInstanceLock:
    """An advisory, OS-level exclusive lock on one file, held for the process
    lifetime.

    Cross-platform via two different primitives behind one interface:
    ``msvcrt.locking`` on Windows, ``fcntl.flock`` on POSIX. Both lock types
    are released automatically when the holding process exits or the file
    descriptor is otherwise closed — including a crash or a SIGKILL — so a
    killed API process never leaves a stale lock for the next boot to trip
    over; there is nothing to clean up by hand.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: IO[str] | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            # msvcrt.locking locks a byte range and refuses an empty file
            # (nothing to range over); ensure at least one byte exists before
            # either platform's lock call. flock (POSIX) locks the whole
            # descriptor regardless of content, so this is harmless there too.
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("0")
                handle.flush()
            self._lock_file(handle)
        except OSError as error:
            handle.close()
            raise SingleInstanceError(
                f"Another process already holds the API lock at {self.path}. "
                "Token revocation, the login/token rate limiters and the "
                "per-upload part lock are in-process state that only one API "
                "instance can enforce for this storage root. If this is a "
                "leftover lock from a process that no longer exists, check for "
                "a stuck osce-ai-marker process before doing anything else; set "
                "ALLOW_MULTIPLE_API_INSTANCES=true only once you have verified "
                "this deployment does not depend on any of that."
            ) from error
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._handle = handle
        logger.info("Acquired the single-API-instance lock at %s.", self.path)

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            self._unlock_file(handle)
        finally:
            handle.close()

    @staticmethod
    def _lock_file(handle: IO[str]) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_file(handle: IO[str]) -> None:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - best-effort on shutdown
            logger.warning("Could not explicitly release the API lock at %s.", handle.name, exc_info=True)
