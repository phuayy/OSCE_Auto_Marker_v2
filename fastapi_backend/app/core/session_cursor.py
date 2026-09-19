from __future__ import annotations

from datetime import datetime

from app.core.pagination_cursor import decode_cursor as _decode_cursor
from app.core.pagination_cursor import encode_cursor as _encode_cursor
from app.core.utils import parse_iso

# Sessions order on a real DateTime column, so the generic codec's raw string
# is parsed into a datetime here before it reaches SessionRepository's query
# — see app/core/pagination_cursor.py's docstring for why the job queue's own
# cursor (a TEXT-ordered column) does not share this parsing step.
_SESSION_ID_MAX_LENGTH = 36


def encode_cursor(row: dict) -> str:
    return _encode_cursor(row["createdAt"], row["id"])


def decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    try:
        decoded = _decode_cursor(cursor, max_id_length=_SESSION_ID_MAX_LENGTH)
    except ValueError as error:
        raise ValueError("Invalid session cursor.") from error
    if decoded is None:
        return None
    raw_created_at, identity = decoded
    created_at = parse_iso(raw_created_at)
    if created_at is None:
        raise ValueError("Invalid session cursor.")
    return created_at, identity
