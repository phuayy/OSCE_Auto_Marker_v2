from __future__ import annotations

from tests.test_routes import build_test_client


def _login(client) -> str:
    return client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]


def _seed_media_file(client) -> None:
    scores_dir = client.app.state.container.settings.paths.output_scores_dir
    scores_dir.mkdir(parents=True, exist_ok=True)
    (scores_dir / "x.json").write_text('{"ok": true}', encoding="utf-8")


def test_media_requires_auth_h1(tmp_path) -> None:
    client = build_test_client(tmp_path)
    _seed_media_file(client)

    # Unauthenticated access to a media artifact is rejected.
    assert client.get("/media/scores/x.json").status_code == 401

    # A valid bearer token grants access.
    token = _login(client)
    ok = client.get("/media/scores/x.json", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200
    assert ok.json() == {"ok": True}


def test_media_accepts_short_lived_stream_ticket_m2(tmp_path) -> None:
    client = build_test_client(tmp_path)
    _seed_media_file(client)
    token = _login(client)

    ticket = client.get("/api/auth/stream-ticket", headers={"Authorization": f"Bearer {token}"}).json()["ticket"]
    # The ticket authorizes media without putting the long-lived token in the URL.
    assert client.get(f"/media/scores/x.json?ticket={ticket}").status_code == 200

    # ...but a stream ticket must NOT be usable as a full API credential.
    assert client.get("/api/sessions", headers={"Authorization": f"Bearer {ticket}"}).status_code == 401


def test_stream_ticket_endpoint_requires_auth(tmp_path) -> None:
    client = build_test_client(tmp_path)
    assert client.get("/api/auth/stream-ticket").status_code == 401


def test_session_events_requires_auth(tmp_path) -> None:
    client = build_test_client(tmp_path)
    # No token and no ticket -> rejected before the stream opens.
    assert client.get("/api/sessions/any/events").status_code == 401


def test_logout_revokes_token_m3(tmp_path) -> None:
    client = build_test_client(tmp_path)
    token = _login(client)
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/sessions", headers=headers).status_code == 200

    logout = client.post("/api/auth/logout", headers=headers)
    assert logout.status_code == 200
    assert logout.json()["revoked"] is True

    # The revoked token can no longer authenticate.
    assert client.get("/api/sessions", headers=headers).status_code == 401


def test_login_is_rate_limited_m1(tmp_path) -> None:
    client = build_test_client(tmp_path)
    limit = client.app.state.container.settings.login_rate_limit_max_attempts

    # Exhaust the per-IP window with wrong-password attempts.
    for _ in range(limit):
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert resp.status_code == 401

    # The next attempt is throttled regardless of credentials.
    throttled = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert throttled.status_code == 429
