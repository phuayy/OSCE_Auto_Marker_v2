from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_replace(
    src: Path,
    dst: Path,
    *,
    attempts: int = 5,
    base_delay: float = 0.05,
) -> None:
    """Atomically move ``src`` onto ``dst``, retrying transient Windows sharing errors.

    ``os.replace`` is the atomic-rename primitive both the JSON record store and
    the object storage use. On Windows it raises ``PermissionError`` — WinError 5
    (access denied) or WinError 32 (sharing violation) — whenever another process
    holds a handle on ``src`` or ``dst`` without ``FILE_SHARE_DELETE``. Antivirus
    real-time scanning, the search indexer, and backup agents all do exactly this
    for the brief moment they read a just-written file, so a busy path (e.g. the
    per-part upload record rewritten dozens of times during a large upload) will
    intermittently collide with a scan and fail.

    The collision is transient: the scanner releases its handle within
    milliseconds. Retry with exponential backoff (0.05s, 0.1s, 0.2s, 0.4s → ~0.75s
    worst case over 5 attempts) so the rename outlasts the scan window without
    stalling the caller. POSIX ``os.replace`` is atomic and never hits this, so the
    first attempt returns immediately there.

    Only ``PermissionError`` is retried — ``FileNotFoundError`` (missing ``src``)
    and any other error are real faults and re-raise at once.
    """
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(base_delay * (2 ** attempt))


def sanitize_file_name(original_name: str | None) -> str:
    extension = Path(original_name or "").suffix.lower()
    stem = Path(original_name or "file").stem
    base = re.sub(r"[^a-zA-Z0-9-_]", "-", stem)
    base = re.sub(r"-+", "-", base).strip("-")[:72]
    return f"{base or 'file'}{extension}"


def normalize_session_name(raw_name: str | None) -> str:
    return str(raw_name or "").strip()


def session_name_key(name: str | None) -> str:
    return normalize_session_name(name).lower()


def clamp_number(value: float | int | str | None, minimum: float, maximum: float) -> float:
    try:
        numeric = float(value if value is not None else minimum)
    except (TypeError, ValueError):
        numeric = minimum
    return min(maximum, max(minimum, numeric))


def format_timestamp(total_seconds: float = 0) -> str:
    clamped = max(0, int(total_seconds or 0))
    hours = clamped // 3600
    minutes = (clamped % 3600) // 60
    seconds = clamped % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"
