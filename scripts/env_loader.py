from __future__ import annotations

import os
from pathlib import Path


def load_env_file(
    root_dir: Path | str,
    file_name: str = ".env",
    *,
    allowed_keys: frozenset[str] | None = None,
) -> bool:
    """Load ``.env`` into this process's environment, for a scorer run by hand.

    A scorer subprocess spawned by the API is started with a curated
    environment (``app/core/subprocess_env.py``'s allowlist plus whatever the
    caller explicitly forwards — see ``LLMSettingsService.subprocess_env_for``),
    deliberately withholding secrets it does not need. Loading the *whole*
    ``.env`` here would readmit every one of them (``AUTH_SECRET``,
    ``DATABASE_URL``, every provider key) the moment this runs, since almost
    none of them are already set in that curated environment — undoing the
    parent's allowlist entirely rather than only serving its original purpose
    (letting a developer run this script directly, with no parent process at
    all). ``allowed_keys``, when given, keeps that convenience while limiting
    the readmission to the names this script actually needs, matching
    ``subprocess_env.py``'s own allowlist shape. ``None`` (the default)
    preserves the original unrestricted behaviour, for callers — a hand-run
    debug tool, never spawned by the API — that have no such curated
    environment to protect in the first place.

    Never overwrites a value already present (from the real environment, or
    from a curated parent): ``.env`` only fills gaps.
    """
    env_path = Path(root_dir) / file_name
    if not env_path.exists():
        return False

    allowed_upper = {key.upper() for key in allowed_keys} if allowed_keys is not None else None

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not (key[0].isalpha() or key[0] == "_"):
            continue
        if not all(character.isalnum() or character == "_" for character in key):
            continue
        if allowed_upper is not None and key.upper() not in allowed_upper:
            continue

        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]

        if os.getenv(key) in {None, ""}:
            os.environ[key] = value

    return True
