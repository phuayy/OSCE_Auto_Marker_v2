"""A CDN in front of this app (Cloudflare Tunnel) must never cache /api or
/media on its own defaults — see app/core/cache_policy.py's module docstring
for why: Cloudflare caches by file extension out of the box, media URLs carry
a short-lived ``?ticket=`` that is part of its cache key, and a response with
no origin Cache-Control gets Cloudflare's own default edge TTL regardless.

Two layers, the same split test_security_headers.py uses: ``default_cache_control``
is a pure function tested directly, then a handful of routes through the real
app (``app.main.build_app``) prove it is actually wired into the response
headers middleware and that a route with its own opinion (the frontend's
hashed assets) is left alone.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.cache_policy import NO_STORE_CACHE_CONTROL, default_cache_control
from app.core.config import Settings
from app.main import build_app
from app.services.container import create_container
from tests.fixtures.mail import RecordingEmailSender

# --- default_cache_control: a pure function ----------------------------------


@pytest.mark.parametrize(
    "path",
    ["/api", "/api/health", "/api/sessions", "/api/sessions/s1/events", "/media/scores/x.json", "/media/source/v.mp4"],
)
def test_api_and_media_paths_get_no_store(path: str) -> None:
    assert default_cache_control(path) == NO_STORE_CACHE_CONTROL


@pytest.mark.parametrize("path", ["/", "/index.html", "/assets/index-abc123.js", "/favicon.svg", "/apiary", "/mediation"])
def test_other_paths_get_no_default(path: str) -> None:
    """Neither a bare frontend route nor a path that merely starts with the
    same letters as /api or /media (``/apiary``, ``/mediation``) should match —
    the prefix check is on a full path segment, not a string prefix."""
    assert default_cache_control(path) is None


# --- wired into the real app -------------------------------------------------


def _client_for(tmp_path: Path) -> TestClient:
    """Mirrors test_security_headers.py's ``_client_for``: the real app
    (``app.main.build_app``), attached to a lightly-initialised container,
    exercising the actual middleware stack rather than a hand-rolled one."""
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
    )
    container = create_container(settings, mailer=RecordingEmailSender())
    asyncio.run(container.artifacts.ensure_storage_layout())
    asyncio.run(container.storage.ensure_layout())
    asyncio.run(container.auth.initialize())
    asyncio.run(container.orm_database.initialize())
    asyncio.run(container.user_admin.ensure_bootstrap_admin())

    app = build_app(settings)
    app.state.container = container
    return TestClient(app)


def test_health_gets_no_store(tmp_path) -> None:
    client = _client_for(tmp_path)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.headers["cache-control"] == NO_STORE_CACHE_CONTROL


def test_unauthenticated_401_gets_no_store(tmp_path) -> None:
    """The security-headers middleware wraps require_auth's own 401, so this
    default must reach a refusal exactly as it reaches a success — a stale
    401 sitting at the edge is not dangerous the way a stale video is, but the
    same wiring covers both and this is the cheap way to prove it does."""
    client = _client_for(tmp_path)
    response = client.get("/api/sessions")
    assert response.status_code == 401
    assert response.headers["cache-control"] == NO_STORE_CACHE_CONTROL


def test_body_too_large_413_gets_no_store(tmp_path) -> None:
    """MaxBodySizeMiddleware runs even further inside the stack than
    require_auth; the security-headers middleware wraps that too."""
    client = _client_for(tmp_path)
    response = client.post(
        "/api/auth/login",
        content=b"x" * (Settings().max_request_body_bytes + 1),
        headers={"Content-Length": str(Settings().max_request_body_bytes + 1)},
    )
    assert response.status_code == 413
    assert response.headers["cache-control"] == NO_STORE_CACHE_CONTROL


def test_media_file_gets_no_store(tmp_path) -> None:
    """StaticFiles sends ETag/Last-Modified but never Cache-Control of its
    own, so the whole /media/* surface depended entirely on this default
    before it existed — exactly the ticketed-URL gap described in
    app/core/cache_policy.py."""
    client = _client_for(tmp_path)
    scores_dir = Path(client.app.state.container.settings.paths.output_scores_dir)
    scores_dir.mkdir(parents=True, exist_ok=True)
    (scores_dir / "s1.json").write_text("{}", encoding="utf-8")

    login = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert login.status_code == 200
    token = login.json()["token"]

    response = client.get("/media/scores/s1.json", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == NO_STORE_CACHE_CONTROL


# --- a route with its own opinion is left alone ------------------------------
#
# /api/events (app/api/routes/events.py) sets "no-cache, no-transform" itself
# on a StreamingResponse. Driving that through TestClient would mean holding
# an SSE stream open, which is exactly the kind of test nobody keeps green in
# CI (test_run_api_proxy_headers.py's docstring makes the same call for a
# comparable case) — so this is unit-level, on the same contract the
# middleware actually relies on: setdefault() never touches a header a route
# already set.


def test_setdefault_never_overrides_a_route_that_already_set_its_own() -> None:
    headers = {"Cache-Control": "no-cache, no-transform"}
    value = default_cache_control("/api/events")
    assert value is not None
    headers.setdefault("Cache-Control", value)
    assert headers["Cache-Control"] == "no-cache, no-transform"
