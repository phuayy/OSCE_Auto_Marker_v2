from __future__ import annotations

import re
from pathlib import Path


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
