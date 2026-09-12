from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.utils import write_text_atomic


def read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def extract_json_object(raw_text: str) -> dict[str, Any]:
    text = str(raw_text or "").strip()
    if not text:
        raise ValueError("No JSON content was returned by scorer process.")

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace < 0 or last_brace <= first_brace:
        raise ValueError("Output did not contain a JSON object.")

    parsed = json.loads(text[first_brace : last_brace + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Parsed JSON was not an object.")
    return parsed


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    write_text_atomic(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
