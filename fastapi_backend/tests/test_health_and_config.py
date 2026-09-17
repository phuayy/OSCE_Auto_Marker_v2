from __future__ import annotations

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
