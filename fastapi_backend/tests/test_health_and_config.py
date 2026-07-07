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


def test_specific_cors_and_protected_media_has_no_warnings() -> None:
    settings = Settings(
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        cors_allow_origins=("https://app.example.edu",),
        protect_media_endpoints=True,
    )
    assert settings.collect_runtime_warnings() == []


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
