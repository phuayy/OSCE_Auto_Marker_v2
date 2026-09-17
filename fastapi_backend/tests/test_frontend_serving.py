"""The API can serve the built frontend itself (SERVE_FRONTEND=true).

What a single-box deployment relies on: the login page loads with no token,
hashed assets are cached for a year while index.html is revalidated, an
unknown route boots the app instead of answering the API's JSON 404, and a
missing chunk still fails loudly. Off by default, and a missing build is a
startup warning rather than a crash.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.frontend import (
    IMMUTABLE_CACHE_CONTROL,
    REVALIDATE_CACHE_CONTROL,
    cache_control_for,
    is_navigation_path,
    mount_frontend,
)
from app.core.config import Settings
from tests.test_routes import build_test_client

INDEX_HTML = "<!doctype html><title>OSCE AI Marker</title><script type=module src=/assets/index-abc123.js></script>"


def write_build(dist_dir: Path) -> None:
    (dist_dir / "assets").mkdir(parents=True)
    (dist_dir / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (dist_dir / "assets" / "index-abc123.js").write_text("console.log('app')", encoding="utf-8")
    (dist_dir / "favicon.svg").write_text("<svg/>", encoding="utf-8")


def frontend_settings(tmp_path: Path, *, enabled: bool = True, dist: str = "") -> Settings:
    return Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        serve_frontend=enabled,
        frontend_dist_dir_override=dist,
    )


def serving_client(tmp_path: Path) -> TestClient:
    """The real auth middleware and API routers, with the frontend mounted
    after them the way main.py does."""
    write_build(tmp_path / "dist")
    client = build_test_client(tmp_path)
    assert mount_frontend(client.app, frontend_settings(tmp_path)) is True
    return client


# --- the mount -------------------------------------------------------------

def test_root_serves_index_without_a_token(tmp_path) -> None:
    client = serving_client(tmp_path)
    response = client.get("/")
    assert response.status_code == 200
    assert "OSCE AI Marker" in response.text
    assert response.headers["cache-control"] == REVALIDATE_CACHE_CONTROL


def test_hashed_assets_are_immutable(tmp_path) -> None:
    client = serving_client(tmp_path)
    response = client.get("/assets/index-abc123.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == IMMUTABLE_CACHE_CONTROL


def test_public_files_keep_default_caching(tmp_path) -> None:
    client = serving_client(tmp_path)
    response = client.get("/favicon.svg")
    assert response.status_code == 200
    assert "immutable" not in response.headers.get("cache-control", "")


def test_unknown_route_boots_the_app(tmp_path) -> None:
    """A path with no file behind it is a navigation: index.html, revalidated."""
    client = serving_client(tmp_path)
    response = client.get("/session/abc")
    assert response.status_code == 200
    assert "OSCE AI Marker" in response.text
    assert response.headers["cache-control"] == REVALIDATE_CACHE_CONTROL


def test_missing_asset_still_404s(tmp_path) -> None:
    """A chunk this deploy no longer has must not come back as HTML — the
    browser would try to execute it, and the chunk error boundary would
    never see the rejection it exists to catch."""
    client = serving_client(tmp_path)
    assert client.get("/assets/index-old999.js").status_code == 404
    assert client.get("/vendor.js").status_code == 404


def test_api_and_media_are_not_shadowed(tmp_path) -> None:
    client = serving_client(tmp_path)
    assert client.get("/api/health").json()["ok"] is True
    # Protected as before: the catch-all did not open the API up.
    assert client.get("/api/sessions").status_code == 401
    assert client.get("/media/scores/nothing.json").status_code == 401


def test_disabled_by_default(tmp_path) -> None:
    write_build(tmp_path / "dist")
    client = build_test_client(tmp_path)
    assert mount_frontend(client.app, frontend_settings(tmp_path, enabled=False)) is False
    assert client.get("/").status_code == 404


def test_dist_dir_override_is_honoured(tmp_path) -> None:
    elsewhere = tmp_path / "builds" / "current"
    write_build(elsewhere)
    app = FastAPI()
    assert mount_frontend(app, frontend_settings(tmp_path, dist=str(elsewhere))) is True
    assert TestClient(app).get("/").status_code == 200


def test_missing_build_mounts_and_warns_instead_of_crashing(tmp_path) -> None:
    settings = frontend_settings(tmp_path)
    app = FastAPI()
    assert mount_frontend(app, settings) is True
    client = TestClient(app)
    assert client.get("/").status_code == 404
    assert client.get("/assets/index-abc123.js").status_code == 404
    warnings = settings.collect_runtime_warnings()
    assert any("SERVE_FRONTEND" in warning and "npm run build" in warning for warning in warnings)
    # A build that lands after boot is served without a restart.
    write_build(tmp_path / "dist")
    assert client.get("/").status_code == 200


def test_present_build_produces_no_frontend_warning(tmp_path) -> None:
    write_build(tmp_path / "dist")
    warnings = frontend_settings(tmp_path).collect_runtime_warnings()
    assert not any("SERVE_FRONTEND" in warning for warning in warnings)


# --- the pure rules --------------------------------------------------------

def test_navigation_paths() -> None:
    assert is_navigation_path("") is True
    assert is_navigation_path("/") is True
    # What Starlette hands over for the root, and on Windows.
    assert is_navigation_path(".") is True
    assert is_navigation_path("session\abc") is True
    assert is_navigation_path("assets\index-abc123.js") is False
    assert is_navigation_path("session/abc") is True
    assert is_navigation_path("rubric") is True
    assert is_navigation_path("favicon.svg") is False
    assert is_navigation_path("assets/index-abc123.js") is False
    # Nothing under assets/ is ever a navigation, extension or not.
    assert is_navigation_path("assets/extensionless") is False


def test_cache_policy() -> None:
    assert cache_control_for("assets/index-abc123.js", served_index=False) == IMMUTABLE_CACHE_CONTROL
    assert cache_control_for("assets\index-abc123.js", served_index=False) == IMMUTABLE_CACHE_CONTROL
    assert cache_control_for("", served_index=True) == REVALIDATE_CACHE_CONTROL
    assert cache_control_for("session/abc", served_index=True) == REVALIDATE_CACHE_CONTROL
    assert cache_control_for("favicon.svg", served_index=False) is None
