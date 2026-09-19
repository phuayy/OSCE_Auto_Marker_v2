"""Opaque keyset-pagination cursor: base64(json([sortKey, id])).

The codec behind every "newest first, `(sortKey, id)` tiebreak" page in the
app. A page like this hands the browser an opaque token for "everything
before this row" instead of an offset, so a page stays correct while rows are
still being inserted underneath it — deleting or adding a row ahead of the
cursor never reshuffles what the next page returns.

The sort key travels as the exact string the caller supplies and comes back
out exactly as given, with no round-trip through a richer type. That is
deliberate: a caller ordering against a ``DateTime`` column (the session
index) needs a real ``datetime`` to compare against and can parse this string
into one; a caller ordering against a TEXT column of ISO-8601 strings (the
job queue, which sorts its timestamps lexicographically on purpose — see
``JobRepository``'s module docstring) must *not* be forced through a
datetime-and-back round trip, because reformatting the boundary value could
shift it off the exact string stored in the row and skip or repeat one.
"""

from __future__ import annotations

import base64
import binascii
import json

MAX_CURSOR_LENGTH = 512
MAX_ID_LENGTH = 64


def encode_cursor(sort_key: str, entity_id: str) -> str:
    value = json.dumps([str(sort_key), str(entity_id)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def decode_cursor(cursor: str | None, *, max_id_length: int = MAX_ID_LENGTH) -> tuple[str, str] | None:
    """Return the ``(sort_key, id)`` pair the cursor encodes, or None for no cursor.

    Raises ``ValueError`` for anything malformed or tampered with — the only
    input here a client controls, so it is validated rather than trusted.
    """
    if cursor is None:
        return None
    try:
        if not cursor or len(cursor) > MAX_CURSOR_LENGTH:
            raise ValueError
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError
        sort_key, entity_id = value
        if not isinstance(sort_key, str) or not sort_key:
            raise ValueError
        if not isinstance(entity_id, str) or not 1 <= len(entity_id) <= max_id_length:
            raise ValueError
        return sort_key, entity_id
    except (ValueError, TypeError, binascii.Error, UnicodeError) as error:
        raise ValueError("Invalid pagination cursor.") from error


__all__ = ["MAX_CURSOR_LENGTH", "MAX_ID_LENGTH", "decode_cursor", "encode_cursor"]
