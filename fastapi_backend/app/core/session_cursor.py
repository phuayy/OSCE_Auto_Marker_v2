from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime

from app.core.utils import parse_iso


def encode_cursor(row: dict) -> str:
    value = json.dumps([row["createdAt"], row["id"]], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if cursor is None:
        return None
    try:
        if not cursor or len(cursor) > 512:
            raise ValueError
        value = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError
        timestamp, identity = value
        if not isinstance(timestamp, str) or not isinstance(identity, str) or not 1 <= len(identity) <= 36:
            raise ValueError
        created_at = parse_iso(timestamp)
        if created_at is None:
            raise ValueError
        return created_at, identity
    except (ValueError, TypeError, binascii.Error, UnicodeError) as error:
        raise ValueError("Invalid session cursor.") from error
