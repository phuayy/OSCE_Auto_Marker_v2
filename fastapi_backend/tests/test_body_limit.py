"""A2: a JSON endpoint with no size contract of its own must not buffer an
unbounded request body.

Two layers, mirroring ``test_security_headers.py``: ``MaxBodySizeMiddleware``
is exercised directly as a plain ASGI middleware with no FastAPI/container
overhead, then the second half proves it is actually wired into the real app
(``app.main.build_app``) ahead of both CORS and auth, and that the one route
built to carry a multi-megabyte body — ``PUT /api/uploads/{id}/parts/{n}`` —
is exempt from it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.core.body_limit import MaxBodySizeMiddleware
from app.core.config import Settings
from app.main import build_app
from app.services.container import create_container
from tests.fixtures.mail import RecordingEmailSender


# --- MaxBodySizeMiddleware: a plain ASGI middleware, no container needed ----


async def _echo(request):
    body = await request.body()
    return PlainTextResponse(str(len(body)))


def _bare_client(max_bytes: int) -> TestClient:
    inner = Starlette(
        routes=[
            Route("/echo", _echo, methods=["POST"]),
            Route("/api/uploads/{upload_id}/parts/{part_number}", _echo, methods=["PUT"]),
        ]
    )
    return TestClient(MaxBodySizeMiddleware(inner, max_bytes=max_bytes))


def test_body_under_the_cap_passes_through() -> None:
    client = _bare_client(max_bytes=1024)
    response = client.post("/echo", content=b"x" * 100)
    assert response.status_code == 200
    assert response.text == "100"


def test_declared_length_over_the_cap_is_rejected_before_reading() -> None:
    client = _bare_client(max_bytes=10)
    response = client.post("/echo", content=b"x" * 1000)
    assert response.status_code == 413
    assert "10" in response.json()["error"] or "MB" in response.json()["error"]


def test_missing_content_length_is_not_rejected() -> None:
    """No declared length to check against; the middleware only refuses what
    it can see up front (see its docstring on the streaming gap this leaves)."""

    def gen():
        yield b"x" * 1000

    client = _bare_client(max_bytes=10)
    response = client.post("/echo", content=gen())
    assert response.status_code == 200


def test_part_upload_route_is_exempt_even_over_the_generic_cap() -> None:
    client = _bare_client(max_bytes=10)
    response = client.put("/api/uploads/session-1/parts/1", content=b"x" * 1000)
    assert response.status_code == 200
    assert response.text == "1000"


def test_a_similarly_shaped_but_different_method_is_not_exempt() -> None:
    client = _bare_client(max_bytes=10)
    # Same path, wrong method: PUT is what carries the big chunk, so the
    # exemption is method-specific — a POST to the same URL gets no free pass.
    response = client.post("/api/uploads/session-1/parts/1", content=b"x" * 1000)
    assert response.status_code == 413


# --- wired into the real app, ahead of CORS and auth alike -------------------


def _client_for(tmp_path: Path, **overrides) -> TestClient:
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
        **overrides,
    )
    container = create_container(settings, mailer=RecordingEmailSender())
    asyncio.run(container.artifacts.ensure_storage_layout())
    asyncio.run(container.storage.ensure_layout())
    asyncio.run(container.auth.initialize())
    asyncio.run(container.orm_database.initialize())

    app = build_app(settings)
    app.state.container = container
    return TestClient(app)


def test_oversized_login_body_is_rejected_before_auth_or_validation(tmp_path) -> None:
    client = _client_for(tmp_path, max_request_body_mb=1)
    response = client.post(
        "/api/auth/login",
        content=b"{" + b'"padding": "' + b"x" * (2 * 1024 * 1024) + b'"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_oversized_body_response_still_carries_security_headers(tmp_path) -> None:
    """add_security_headers must wrap the 413 short-circuit too, the same way
    it wraps a 401 from require_auth (see test_security_headers.py)."""
    client = _client_for(tmp_path, max_request_body_mb=1)
    response = client.post(
        "/api/auth/login",
        content=b"x" * (2 * 1024 * 1024),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.headers["x-content-type-options"] == "nosniff"


def test_ordinary_login_request_is_unaffected(tmp_path) -> None:
    client = _client_for(tmp_path, max_request_body_mb=1)
    response = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert response.status_code != 413


@pytest.mark.parametrize("part_number", [1, 2])
def test_upload_part_route_bypasses_the_generic_cap_in_the_real_app(tmp_path, part_number) -> None:
    """The route's own (larger) cap still applies — this only proves the
    generic 1 MB cap configured here does not also fire on it first."""
    client = _client_for(tmp_path, max_request_body_mb=1, upload_part_size_mb=8)
    response = client.put(
        f"/api/uploads/does-not-exist/parts/{part_number}",
        content=b"x" * (2 * 1024 * 1024),
        headers={"Content-Type": "application/octet-stream"},
    )
    # Not 413: the generic cap did not intercept it. The route itself then
    # fails on ownership/existence (require_upload_owner), which is a
    # different, expected failure for a session id that was never created.
    assert response.status_code != 413
