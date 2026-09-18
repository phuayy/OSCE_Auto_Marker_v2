"""A6: /docs, /redoc and /openapi.json map the whole API — every admin route
included — with no authentication of their own. Gated behind API_DOCS_ENABLED,
off by default.
"""

from __future__ import annotations

from app.core.config import Settings
from app.main import build_app


def _settings(tmp_path, **overrides) -> Settings:
    defaults = {
        "root_dir": tmp_path,
        "backend_root": tmp_path,
        "ffmpeg_bin": "ffmpeg",
        "ffprobe_bin": "ffprobe",
        "scorer_python_bin": "python",
        "app_database_url": "",
        "database_url": "",
    }
    return Settings(**{**defaults, **overrides})


def test_docs_are_disabled_by_default(tmp_path) -> None:
    app = build_app(_settings(tmp_path))
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None


def test_docs_are_served_when_explicitly_enabled(tmp_path) -> None:
    app = build_app(_settings(tmp_path, api_docs_enabled=True))
    assert app.docs_url == "/docs"
    assert app.redoc_url == "/redoc"
    assert app.openapi_url == "/openapi.json"


def test_enabling_docs_produces_a_runtime_warning(tmp_path) -> None:
    warnings = _settings(tmp_path, api_docs_enabled=True).collect_runtime_warnings()
    assert any("API_DOCS_ENABLED" in warning for warning in warnings)


def test_docs_disabled_produces_no_warning() -> None:
    # Mirrors test_health_and_config.py::test_production_shaped_config_has_no_warnings
    # (no root_dir/backend_root override, so human-detector path resolution
    # finds the real script rather than a tmp_path stand-in).
    settings = Settings(
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        cors_allow_origins=("https://app.example.edu",),
        protect_media_endpoints=True,
        email_backend="smtp",
        smtp_host="smtp.example.edu",
        email_from="OSCE AI Marker <no-reply@example.edu>",
        app_public_url="https://app.example.edu",
    )
    assert settings.collect_runtime_warnings() == []
