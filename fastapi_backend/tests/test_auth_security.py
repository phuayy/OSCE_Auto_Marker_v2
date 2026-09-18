from __future__ import annotations

import asyncio

import pytest
from starlette.requests import Request

from app.api.dependencies import authorize_request
from app.core.config import Settings
from app.database.orm import OrmDatabase
from app.domain.users import UserRole, UserStatus
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService
from app.services.change_feed_service import ChangeFeedService
from app.services.user_directory import UserDirectory, UserSnapshot

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


@pytest.mark.parametrize("path", [
    "/api/sessions", "/api/auth/me", "/api/auth/stream-ticket",
    "/api/events", "/api/sessions/any/events", "/media/scores/x.json",
])
def test_query_bearer_is_never_accepted(tmp_path, path) -> None:
    client = build_test_client(tmp_path)
    token = _login(client)
    assert client.get(path, params={"token": token}).status_code == 401


def test_ticket_cannot_authorize_regular_api_or_mutations(tmp_path) -> None:
    client = build_test_client(tmp_path)
    token = _login(client)
    ticket = client.get("/api/auth/stream-ticket", headers={"Authorization": f"Bearer {token}"}).json()["ticket"]
    assert client.get("/api/sessions", params={"ticket": ticket}).status_code == 401
    assert client.post("/api/sessions/s1/rerun", params={"ticket": ticket}).status_code == 401


@pytest.mark.parametrize('path', ['/api/events', '/api/sessions/s1/events', '/media/scores/x.json'])
def test_tickets_authorize_only_read_methods(tmp_path, path) -> None:
    client = build_test_client(tmp_path)
    token = _login(client)
    ticket = client.get('/api/auth/stream-ticket', headers={'Authorization': f'Bearer {token}'}).json()['ticket']
    for method in ('GET', 'HEAD', 'POST', 'DELETE'):
        request = Request({'type': 'http', 'method': method, 'path': path,
            'headers': [], 'query_string': f'ticket={ticket}'.encode()})
        allowed, _ = asyncio.run(authorize_request(request, client.app.state.container, protect_media=True))
        assert allowed is (method in {'GET', 'HEAD'})


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


def test_logout_revocation_reaches_a_second_process(tmp_path) -> None:
    """The actual bug A3 closes: a process-local revocation registry never
    reached a second process. Two AuthService instances over one database and
    one auth-secret file stand in for two API processes (a Hatchet worker
    aside, which never verifies tokens) sharing one storage root."""

    async def scenario() -> None:
        settings = Settings(
            root_dir=tmp_path,
            backend_root=tmp_path,
            auth_bcrypt_rounds=4,
            ffmpeg_bin="ffmpeg",
            ffprobe_bin="ffprobe",
            scorer_python_bin="python",
            app_database_url="",
            database_url="",
        )
        database = OrmDatabase(tmp_path / "app.sqlite3")

        def _build_process() -> AuthService:
            users = UserRepository(database)
            changes = ChangeFeedService(database)
            directory = UserDirectory(users, changes=changes)
            return AuthService(settings, users=users, directory=directory, changes=changes)

        process_a = _build_process()
        process_b = _build_process()
        await process_a.initialize()
        await process_b.initialize()

        record = await UserRepository(database).create(
            username="marker",
            email="marker@example.edu",
            display_name="",
            role=UserRole.MARKER,
            status=UserStatus.ACTIVE,
            password_hash=None,
        )
        snapshot = UserSnapshot(
            id=record.id, username=record.username, role=UserRole.MARKER,
            status=UserStatus.ACTIVE, token_version=record.token_version,
        )
        token = process_a.issue_session_token(snapshot)["token"]

        # Valid from either process before any revocation.
        assert await process_a.verify_token(token) is not None
        assert await process_b.verify_token(token) is not None

        # Logout happens to land on process A.
        assert await process_a.revoke_token(token) is True

        # The bug: a purely in-process registry would leave process B unaware.
        assert await process_a.verify_token(token) is None
        assert await process_b.verify_token(token) is None

    asyncio.run(scenario())


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
