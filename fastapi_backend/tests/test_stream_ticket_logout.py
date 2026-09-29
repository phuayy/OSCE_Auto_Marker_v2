"""Audit 2026-09-29 follow-up: a stream ticket must die with the bearer token
that minted it.

Tickets carry their own ``tokenId``, so revoking the bearer on logout left
every ticket minted from it valid until its own expiry (600 s by default) —
long enough for a copied ``?ticket=`` media URL to keep working after the
user signed out.
"""

from __future__ import annotations

from tests.test_auth_security import _login, _seed_media_file
from tests.test_routes import build_test_client


def test_logout_revokes_the_tickets_the_token_minted(tmp_path) -> None:
    client = build_test_client(tmp_path)
    _seed_media_file(client)
    token = _login(client)
    headers = {"Authorization": f"Bearer {token}"}
    ticket = client.get("/api/auth/stream-ticket", headers=headers).json()["ticket"]
    assert client.get(f"/media/scores/x.json?ticket={ticket}").status_code == 200

    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get(f"/media/scores/x.json?ticket={ticket}").status_code == 401


def test_logging_out_one_session_leaves_another_sessions_tickets_alone(tmp_path) -> None:
    client = build_test_client(tmp_path)
    _seed_media_file(client)
    first, second = _login(client), _login(client)
    ticket = client.get("/api/auth/stream-ticket", headers={"Authorization": f"Bearer {second}"}).json()["ticket"]

    assert client.post("/api/auth/logout", headers={"Authorization": f"Bearer {first}"}).status_code == 200
    assert client.get(f"/media/scores/x.json?ticket={ticket}").status_code == 200
