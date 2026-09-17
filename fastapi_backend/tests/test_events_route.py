from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api.dependencies import authorize_request, is_stream_path
from app.api.routes import auth, events
from app.core.config import Settings
from app.database.change_tracking import TRACKED_TABLES
from app.services.change_feed_service import ChangeFeedService
from app.database.orm import OrmDatabase
from app.services.container import create_container


def _build_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        auth_bcrypt_rounds=4,
        default_admin_password="admin",
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        # Force SQLite so tests never touch a real database from the env var.
        app_database_url="",
        database_url="",
    )
    container = create_container(settings)
    asyncio.run(container.artifacts.ensure_storage_layout())
    asyncio.run(container.storage.ensure_layout())
    asyncio.run(container.auth.initialize())
    asyncio.run(container.orm_database.initialize())
    asyncio.run(container.user_admin.ensure_bootstrap_admin())

    app = FastAPI()
    app.state.container = container

    @app.middleware("http")
    async def require_auth(request: Request, call_next):
        allowed, payload = await authorize_request(
            request,
            container,
            protect_media=settings.protect_media_endpoints,
        )
        if not allowed:
            return JSONResponse(status_code=401, content={"error": "Authentication required."})
        if payload is not None:
            request.state.auth_user = payload
        return await call_next(request)

    app.include_router(auth.router, prefix="/api")
    app.include_router(events.router, prefix="/api")
    return TestClient(app)


def _login(client: TestClient) -> str:
    response = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert response.status_code == 200
    return response.json()["token"]


def _ticket(client: TestClient, token: str) -> str:
    return client.get(
        "/api/auth/stream-ticket", headers={"Authorization": f"Bearer {token}"}
    ).json()["ticket"]


def test_events_endpoints_require_authentication(tmp_path) -> None:
    client = _build_client(tmp_path)
    assert client.get("/api/events/versions").status_code == 401
    assert client.get("/api/events").status_code == 401


def test_versions_endpoint_reports_counters_for_tracked_tables(tmp_path) -> None:
    client = _build_client(tmp_path)
    token = _login(client)

    response = client.get("/api/events/versions", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    # Asserted against the module constant rather than a hardcoded set, so
    # tracking a new table updates one place instead of failing here.
    assert set(body["versions"]) == set(TRACKED_TABLES)
    assert "notifications" in body["versions"], (
        "notifications must be tracked or worker-raised notifications never "
        "reach the browser (see TRACKED_TABLES)"
    )
    assert body["push"] is False  # SQLite in tests: counters, no LISTEN/NOTIFY
    # Superset assertion: the diagnostics payload gains fields over time, and
    # pinning an exact set turns every added metric into a false failure.
    assert {"hits", "misses", "entries"} <= set(body["cache"])


def test_event_stream_path_accepts_stream_tickets() -> None:
    """EventSource cannot send an Authorization header, so /api/events must be
    on the ticket-accepting list alongside the per-session stream."""
    assert is_stream_path("/api/events") is True
    assert is_stream_path("/api/sessions/abc/events") is True
    # Everything else must still demand a real bearer token.
    assert is_stream_path("/api/sessions") is False
    assert is_stream_path("/api/events/versions") is False


def test_stream_ticket_is_not_accepted_as_an_api_credential(tmp_path) -> None:
    """A ticket unlocks streams only; it must not work as a general API token."""
    client = _build_client(tmp_path)
    token = _login(client)
    ticket = _ticket(client, token)

    assert client.get(f"/api/events/versions?token={ticket}").status_code == 401


def test_stream_opens_with_a_ready_frame_then_carries_changes(tmp_path) -> None:
    """Drive the SSE generator directly: an HTTP stream never completes, so a
    TestClient request would block rather than assert anything useful."""

    async def _run() -> None:
        database = OrmDatabase(tmp_path / "app.sqlite3")
        await database.initialize()
        changes = ChangeFeedService(database, poll_interval_seconds=0.05)
        await changes.start(push_enabled=False)
        stream = changes.stream()
        try:
            assert "retry:" in await anext(stream)

            ready = await anext(stream)
            assert "event: ready" in ready
            # A reconnecting client needs the version snapshot to know what it
            # missed while it was disconnected.
            assert "versions" in ready

            # An application event raised anywhere in the process must reach the
            # browser on this same connection, under its own SSE event name.
            changes.publish_event("notification", {"notification": {"id": "n1"}})
            frame = await asyncio.wait_for(anext(stream), timeout=5.0)
            assert "event: notification" in frame
            assert "n1" in frame
        finally:
            await stream.aclose()
            await changes.stop()
            await database.shutdown()

    asyncio.run(_run())
