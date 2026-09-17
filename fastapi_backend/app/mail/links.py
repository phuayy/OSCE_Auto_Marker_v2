"""The links the emails carry, spelled once.

The browser routes these resolve to live in ``src/lib/navigation.js``; the two
files have to agree, and ``tests/test_user_admin.py`` pins the shape. The token
rides in the URL *fragment*: a browser never sends the fragment to the server,
so the raw token appears in no access log along the way.
"""

from __future__ import annotations

from urllib.parse import quote


def _base(public_url: str) -> str:
    return str(public_url or "").strip().rstrip("/")


def invite_link(public_url: str, token: str) -> str:
    return f"{_base(public_url)}/#/accept-invite/{quote(token, safe='')}"


def password_reset_link(public_url: str, token: str) -> str:
    return f"{_base(public_url)}/#/reset-password/{quote(token, safe='')}"
