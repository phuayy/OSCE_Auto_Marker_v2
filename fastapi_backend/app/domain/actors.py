"""Who did it: the identity a request acts as, and how it is recorded.

Two things a service needs to know about the person behind a request, and
neither should be re-derived from the raw auth payload at every call site:

* an :class:`Actor` — the value routes hand to services (``actor=`` keyword)
  so a service can say who invited whom, who uploaded a session, who rotated a
  key, without ever seeing a request object;
* a **provenance snapshot** — what is *stored* on a record that person
  created. It is a copy of the identity as it was at that moment, not a
  reference: an account can be renamed, disabled or deleted later, and a
  session uploaded last term must still say who uploaded it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The key under which a session (and anything else that records a creator)
# carries its provenance snapshot: {"userId", "username", "displayName"}.
PROVENANCE_KEY = "createdBy"


@dataclass(frozen=True)
class Actor:
    user_id: str
    username: str
    display_name: str = ""

    @classmethod
    def from_auth_payload(cls, payload: Any) -> "Actor | None":
        """The actor behind a verified bearer token, or None for an
        unauthenticated request (the emailed-link endpoints)."""
        if not isinstance(payload, dict):
            return None
        user_id = str(payload.get("sub") or "").strip()
        username = str(payload.get("username") or "").strip()
        if not user_id or not username:
            return None
        return cls(user_id=user_id, username=username, display_name=str(payload.get("displayName") or "").strip())

    @property
    def label(self) -> str:
        """How the person is shown: their name, else their login handle."""
        return self.display_name or self.username

    def to_provenance(self) -> dict[str, str]:
        """The snapshot a created record keeps."""
        return {"userId": self.user_id, "username": self.username, "displayName": self.display_name}


def provenance_of(record: Any) -> dict[str, str] | None:
    """The creator snapshot on a record, normalised, or None when it has none
    (a session created before creators were recorded, or by a process with no
    actor). Tolerant of any shape: a malformed value reads as none."""
    if not isinstance(record, dict):
        return None
    raw = record.get(PROVENANCE_KEY)
    if not isinstance(raw, dict):
        return None
    user_id = str(raw.get("userId") or "").strip()
    username = str(raw.get("username") or "").strip()
    if not user_id and not username:
        return None
    return {
        "userId": user_id,
        "username": username,
        "displayName": str(raw.get("displayName") or "").strip(),
    }


def provenance_user_id(record: Any) -> str | None:
    """The creator's user id, for the indexed column that mirrors the snapshot."""
    snapshot = provenance_of(record)
    return (snapshot or {}).get("userId") or None
