"""B3: baseline security response headers.

Two layers: ``apply_security_headers`` is a pure function over a header
mapping and a ``Settings`` value, tested directly with no HTTP involved; the
second half proves it is actually wired into the real app every request goes
through (``app.main.build_app``), on both a normal route and one the auth
middleware short-circuits with a 401 — a header that only shows up on success
would not be defense in depth.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.security_headers import apply_security_headers, build_content_security_policy
from app.main import build_app
from app.services.container import create_container
from tests.fixtures.mail import RecordingEmailSender


# --- apply_security_headers: a pure function over a header mapping ----------


def test_baseline_headers_are_set_by_default() -> None:
    headers: dict[str, str] = {}
    apply_security_headers(headers, Settings(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", scorer_python_bin="python"))
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert "Strict-Transport-Security" not in headers


def test_setdefault_never_overrides_a_header_already_set() -> None:
    """A route with a deliberate reason to set one of these itself (none does
    today) must not be silently second-guessed."""
    headers = {"X-Frame-Options": "SAMEORIGIN"}
    apply_security_headers(headers, Settings(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", scorer_python_bin="python"))
    assert headers["X-Frame-Options"] == "SAMEORIGIN"


def test_hsts_is_off_unless_explicitly_enabled() -> None:
    settings = Settings(
        ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", scorer_python_bin="python", hsts_enabled=True, hsts_max_age_seconds=3600
    )
    headers: dict[str, str] = {}
    apply_security_headers(headers, settings)
    assert headers["Strict-Transport-Security"] == "max-age=3600; includeSubDomains"


def test_content_security_policy_override_replaces_the_default() -> None:
    settings = Settings(
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        content_security_policy_override="default-src 'none'",
    )
    assert build_content_security_policy(settings) == "default-src 'none'"
    headers: dict[str, str] = {}
    apply_security_headers(headers, settings)
    assert headers["Content-Security-Policy"] == "default-src 'none'"


# --- wired into the real app, success and refusal responses alike -----------


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
    )
    container = create_container(settings, mailer=RecordingEmailSender())
    asyncio.run(container.artifacts.ensure_storage_layout())
    asyncio.run(container.storage.ensure_layout())
    asyncio.run(container.auth.initialize())
    asyncio.run(container.orm_database.initialize())

    # The real app (app.main.build_app), attached to a lightly-initialised
    # container the way tests/test_routes.build_test_client attaches one to a
    # hand-rolled app — this is the actual middleware stack production runs,
    # security headers included, without paying for full lifespan startup
    # (migrations, the rubric parse, upload recovery) that this test does not
    # need.
    app = build_app(settings)
    app.state.container = container
    return TestClient(app)


def test_security_headers_are_present_on_a_successful_response(tmp_path) -> None:
    client = _client_for(tmp_path)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_security_headers_are_present_on_an_unauthenticated_401(tmp_path) -> None:
    """The auth middleware short-circuits before a route runs; the headers
    middleware must still wrap that response, not only a route's own."""
    client = _client_for(tmp_path)
    response = client.get("/api/sessions")
    assert response.status_code == 401
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" in {key.lower() for key in response.headers.keys()}
