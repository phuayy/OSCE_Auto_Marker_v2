"""D2: a cross-origin preflight must never be refused by ``require_auth``.

A browser's own preflight (`OPTIONS` + `Origin` + `Access-Control-Request-Method`)
carries no bearer token — it precedes the real request and the browser builds
it itself. `CORSMiddleware` is meant to answer it directly, before any route
or auth check runs. That only holds if `CORSMiddleware` is registered *after*
`require_auth` (Starlette's user middleware nests in reverse registration
order, so the later-registered one is outermost and sees the request first).
Getting the order backwards makes every preflight to a protected route under
a cross-origin dev setup (`DEV_SERVER_HOST` != `API_HOST`, or any deployment
that doesn't set `SERVE_FRONTEND=true`) come back 401, and the browser never
sends the real request at all — see the "Auth middleware ordering vs CORS"
finding in the architecture audit.

This builds the real app (`app.main.build_app`), the same way
`test_security_headers.py` does, so it exercises the actual middleware stack
rather than a hand-rolled test double.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import build_app
from app.services.container import create_container
from tests.fixtures.mail import RecordingEmailSender

ALLOWED_ORIGIN = "https://app.example.edu"


def _client_for(tmp_path: Path) -> TestClient:
    settings = Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        auth_bcrypt_rounds=4,
        default_admin_password="admin",
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        app_database_url="",
        database_url="",
        cors_allow_origins=(ALLOWED_ORIGIN,),
    )
    container = create_container(settings, mailer=RecordingEmailSender())
    asyncio.run(container.artifacts.ensure_storage_layout())
    asyncio.run(container.storage.ensure_layout())
    asyncio.run(container.auth.initialize())
    asyncio.run(container.orm_database.initialize())

    app = build_app(settings)
    app.state.container = container
    return TestClient(app)


def test_preflight_to_a_protected_route_is_answered_by_cors_not_auth(tmp_path) -> None:
    client = _client_for(tmp_path)
    response = client.options(
        "/api/sessions",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    # CORSMiddleware answers a preflight itself with 200; require_auth must
    # never get a chance to turn it into a 401.
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN


def test_preflight_to_an_admin_route_is_also_answered_by_cors_not_auth(tmp_path) -> None:
    client = _client_for(tmp_path)
    response = client.options(
        "/api/admin/users",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN


def test_actual_cross_origin_request_still_requires_auth(tmp_path) -> None:
    """Reordering must not weaken auth itself — only the preflight bypasses
    it. The real GET, sent after the preflight would have succeeded, is still
    a 401 with no bearer token."""
    client = _client_for(tmp_path)
    response = client.get("/api/sessions", headers={"Origin": ALLOWED_ORIGIN})
    assert response.status_code == 401
    # CORSMiddleware still tags the refusal for a browser that did send the
    # real request (e.g. a same-origin-policy-exempt tool, or after a
    # successful preflight the browser already ran).
    assert response.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN


def test_security_headers_still_wrap_a_preflight_response(tmp_path) -> None:
    """add_security_headers must stay outermost after the reorder."""
    client = _client_for(tmp_path)
    response = client.options(
        "/api/sessions",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
