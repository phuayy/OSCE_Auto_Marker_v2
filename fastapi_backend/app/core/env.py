from __future__ import annotations

import os


def read_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    token = raw.strip().lower()
    if token in {"1", "true", "yes", "y", "on"}:
        return True
    if token in {"0", "false", "no", "n", "off"}:
        return False
    return default


def read_int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def read_float_env(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def read_csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    values = tuple(item.strip() for item in os.getenv(name, "").split(",") if item.strip())
    return values or default
