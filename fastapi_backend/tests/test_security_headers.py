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
import base64
import hashlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.security_headers import (
    THEME_BOOT_SCRIPT_HASH,
    apply_security_headers,
    build_content_security_policy,
)
from app.main import build_app
from app.services.container import create_container
from tests.fixtures.mail import RecordingEmailSender


def _headers_settings() -> Settings:
    return Settings(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", scorer_python_bin="python")


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


# --- F4: the policy has to cover index.html's own inline script -------------

# The frontend entry document, from this test file: tests/ -> fastapi_backend/
# -> the checkout root.
REPO_ROOT = Path(__file__).resolve().parents[2]
# A <script> with no src= is what the browser treats as inline, and so what a
# hash has to cover. Mirrors the extraction in test/theme.test.mjs.
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL)


def _inline_script_hashes(html: str) -> list[str]:
    """The CSP source expression for each inline script, as a browser computes
    it: SHA-256 over the exact bytes between the tags, base64-encoded."""
    return [
        "sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode("ascii")
        for body in INLINE_SCRIPT.findall(html)
    ]


def _script_src(policy: str) -> str:
    return next(part for part in policy.split("; ") if part.startswith("script-src"))


def test_csp_covers_every_inline_script_in_index_html() -> None:
    """``script-src 'self'`` refuses inline scripts, and index.html carries one:
    the theme boot that adds the dark class before the first paint. Blocked, a
    dark-theme user gets a white flash on every load.

    The digest is recomputed from index.html here rather than restated, so
    editing the boot script — or adding a second inline one — fails this test
    instead of failing silently in a browser no test drives.
    """
    index_html = REPO_ROOT / "index.html"
    if not index_html.is_file():
        pytest.skip(f"no frontend entry document at {index_html} (backend-only checkout)")

    hashes = _inline_script_hashes(index_html.read_text(encoding="utf-8"))
    assert hashes, "index.html no longer has an inline script; THEME_BOOT_SCRIPT_HASH is now dead weight"
    assert THEME_BOOT_SCRIPT_HASH in hashes, (
        "THEME_BOOT_SCRIPT_HASH is stale: index.html's inline script(s) now hash to "
        f"{hashes}. Update the constant in app/core/security_headers.py."
    )

    script_src = _script_src(build_content_security_policy(_headers_settings()))
    for digest in hashes:
        assert f"'{digest}'" in script_src, f"script-src does not admit index.html's inline script ({digest})"


def test_csp_admits_the_boot_script_without_opening_up_inline_script() -> None:
    """A hash is the whole point: this exact script runs and nothing else
    injected does. ``'unsafe-inline'`` would have been the lazy fix and is
    also the one that gives up the protection."""
    script_src = _script_src(build_content_security_policy(_headers_settings()))
    assert "'unsafe-inline'" not in script_src
    assert "'unsafe-eval'" not in script_src
    assert "'self'" in script_src


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
