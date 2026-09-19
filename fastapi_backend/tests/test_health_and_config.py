from __future__ import annotations

import pytest

from app.core.config import Settings
from tests.test_routes import build_test_client


# --- L4: readiness probe ----------------------------------------------------

def test_readiness_reports_database_and_storage(tmp_path) -> None:
    client = build_test_client(tmp_path)
    # Readiness is unauthenticated (orchestrator probe).
    response = client.get("/api/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["checks"]["database"] is True
    assert body["checks"]["storage"] is True
    # Binary availability is reported (informational, not required).
    for key in ("ffmpeg", "ffprobe", "whisperx"):
        assert key in body["checks"]
    # Counters only, for every cache the scoring/auth hot path keeps warm.
    for key in (
        "providerCredentials",
        "appSettings",
        "userSettings",
        "customProviders",
        "userDirectory",
        "tokenRevocations",
    ):
        assert key in body["caches"]


def test_readiness_fails_when_counter_table_disappears(tmp_path) -> None:
    import asyncio

    client = build_test_client(tmp_path)
    database = client.app.state.container.orm_database

    async def break_tracking() -> None:
        async with database.engine.begin() as connection:
            await connection.exec_driver_sql("DROP TABLE table_versions")

    asyncio.run(break_tracking())
    response = client.get("/api/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["database"] is True
    assert response.json()["checks"]["changeTracking"] is False
    assert client.get("/api/health").status_code == 200


def test_readiness_fails_when_a_trigger_disappears(tmp_path) -> None:
    import asyncio

    client = build_test_client(tmp_path)
    database = client.app.state.container.orm_database

    async def break_tracking() -> None:
        async with database.engine.begin() as connection:
            await connection.exec_driver_sql("DROP TRIGGER trg_users_change_update")

    asyncio.run(break_tracking())
    response = client.get("/api/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["changeTracking"] is False


def test_liveness_still_open(tmp_path) -> None:
    client = build_test_client(tmp_path)
    assert client.get("/api/health").json()["ok"] is True


# --- L5: configuration warnings --------------------------------------------

def test_wildcard_cors_produces_warning() -> None:
    settings = Settings(
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        cors_allow_origins=("*",),
    )
    warnings = settings.collect_runtime_warnings()
    assert any("CORS_ALLOW_ORIGINS" in warning for warning in warnings)


def test_production_shaped_config_has_no_warnings() -> None:
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


def test_console_mail_backend_and_plain_public_url_produce_warnings() -> None:
    """Invitations that go to a log, or links sent over http, are worth a line
    at boot — not a refusal, because a laptop deployment is exactly that."""
    settings = Settings(
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        cors_allow_origins=("https://app.example.edu",),
        protect_media_endpoints=True,
        email_backend="console",
        app_public_url="http://osce.internal",
    )
    warnings = settings.collect_runtime_warnings()
    assert any("EMAIL_BACKEND=console" in warning for warning in warnings)
    assert any("APP_PUBLIC_URL" in warning for warning in warnings)


def test_unprotected_media_produces_warning() -> None:
    settings = Settings(
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        cors_allow_origins=("https://app.example.edu",),
        protect_media_endpoints=False,
    )
    warnings = settings.collect_runtime_warnings()
    assert any("PROTECT_MEDIA_ENDPOINTS" in warning for warning in warnings)


# --- production refuses to start, rather than merely warn -------------------


def test_unprotected_media_is_only_a_warning_outside_production() -> None:
    """A local checkout or demo box must never be surprised by a startup
    crash over a default it did not touch."""
    settings = Settings(
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        environment="development",
        protect_media_endpoints=False,
    )
    assert settings.startup_fatal_errors() == []


def test_unprotected_media_is_fatal_in_production() -> None:
    settings = Settings(
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        environment="production",
        protect_media_endpoints=False,
    )
    errors = settings.startup_fatal_errors()
    assert any("PROTECT_MEDIA_ENDPOINTS" in error for error in errors)


def test_protected_media_in_production_has_no_fatal_errors() -> None:
    settings = Settings(
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        environment="production",
        protect_media_endpoints=True,
    )
    assert settings.startup_fatal_errors() == []
    assert settings.is_production is True


def test_environment_is_development_by_default() -> None:
    settings = Settings(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", scorer_python_bin="python")
    assert settings.is_production is False


def test_container_startup_refuses_a_fatal_configuration(tmp_path) -> None:
    """The container, not just the Settings value, actually refuses to boot."""
    import asyncio

    from app.services.container import create_container

    settings = Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        app_database_url="",
        database_url="",
        environment="production",
        protect_media_endpoints=False,
    )
    container = create_container(settings)
    try:
        with pytest.raises(RuntimeError, match="PROTECT_MEDIA_ENDPOINTS"):
            asyncio.run(container.startup())
    finally:
        asyncio.run(container.shutdown())


# --- where the API listens ---------------------------------------------------

def test_bind_defaults_to_loopback(monkeypatch) -> None:
    """Nothing on the network reaches a half-set-up instance unless asked."""
    from app.core.config import DEFAULT_API_HOST, DEFAULT_API_PORT, server_bind_from_env

    monkeypatch.delenv("API_HOST", raising=False)
    monkeypatch.delenv("API_PORT", raising=False)
    assert server_bind_from_env() == (DEFAULT_API_HOST, DEFAULT_API_PORT)
    assert Settings(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", scorer_python_bin="python").binds_loopback_only


def test_bind_reads_api_host_and_port(monkeypatch) -> None:
    from app.core.config import server_bind_from_env

    monkeypatch.setenv("API_HOST", " 0.0.0.0 ")
    monkeypatch.setenv("API_PORT", "9000")
    assert server_bind_from_env() == ("0.0.0.0", 9000)
    settings = Settings(
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        api_host="0.0.0.0",
    )
    assert settings.binds_loopback_only is False


def test_bind_ignores_a_blank_host(monkeypatch) -> None:
    """``API_HOST=`` left empty in .env means the default, not bind to ''."""
    from app.core.config import DEFAULT_API_HOST, server_bind_from_env

    monkeypatch.setenv("API_HOST", "")
    assert server_bind_from_env()[0] == DEFAULT_API_HOST
