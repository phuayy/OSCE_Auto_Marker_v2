from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app.api.dependencies import authorize_request
from app.api.routes import async_uploads, auth, health, jobs, sessions, uploads
from app.core.config import Settings
from app.core.exceptions import AppError
from app.services.container import create_container


def build_test_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        auth_bcrypt_rounds=4,
        default_admin_password="admin",
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        # Force SQLite so tests never hit a real database from APP_DATABASE_URL env var.
        app_database_url="",
        database_url="",
    )
    container = create_container(settings)
    asyncio.run(container.artifacts.ensure_storage_layout())
    asyncio.run(container.storage.ensure_layout())
    asyncio.run(container.auth.initialize())

    app = FastAPI()
    app.state.container = container

    # Mirror the production middleware exactly via the shared authorizer so the
    # tests exercise the real media/SSE/token logic.
    @app.middleware("http")
    async def require_auth(request: Request, call_next):
        allowed, payload = authorize_request(
            request,
            container,
            protect_media=settings.protect_media_endpoints,
        )
        if not allowed:
            return JSONResponse(status_code=401, content={"error": "Authentication required."})
        if payload is not None:
            request.state.auth_user = payload
        return await call_next(request)

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.message})

    app.mount(
        "/media/scores",
        StaticFiles(directory=str(settings.paths.output_scores_dir), check_dir=False),
        name="media-scores",
    )
    app.include_router(health.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(uploads.router, prefix="/api")
    app.include_router(async_uploads.router, prefix="/api")
    app.include_router(jobs.router, prefix="/api")
    app.include_router(sessions.router, prefix="/api")
    return TestClient(app)


def test_login_success_and_failure(tmp_path) -> None:
    client = build_test_client(tmp_path)
    ok = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert ok.status_code == 200
    assert ok.json()["username"] == "admin"

    failed = client.post("/api/auth/login", json={"username": "admin", "password": "bad"})
    assert failed.status_code == 401


def test_protected_route_rejects_missing_token(tmp_path) -> None:
    client = build_test_client(tmp_path)
    response = client.get("/api/sessions")
    assert response.status_code == 401
    assert response.json()["error"] == "Authentication required."


def test_session_listing_returns_seeded_session(tmp_path) -> None:
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    asyncio.run(
        client.app.state.container.sessions.write(
            {
                "id": "s1",
                "name": "Fixture",
                "createdAt": "2026-01-01T00:00:00Z",
                "status": "uploaded",
                "outputs": {"videoClips": []},
            }
        )
    )
    response = client.get("/api/sessions", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["sessions"][0]["id"] == "s1"


def test_async_upload_initiate_and_local_part(tmp_path) -> None:
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    initiate = client.post(
        "/api/uploads/initiate",
        headers=headers,
        json={
            "workflow": "standard",
            "autoProcess": True,
            "files": [
                {
                    "kind": "video",
                    "originalName": "station.mp4",
                    "mimeType": "video/mp4",
                    "sizeBytes": 5,
                },
                {
                    "kind": "caseStudy",
                    "originalName": "case.pdf",
                    "mimeType": "application/pdf",
                    "sizeBytes": 4,
                },
            ],
        },
    )
    assert initiate.status_code == 201
    body = initiate.json()
    assert body["strategy"] == "local_multipart"
    job_status = client.get(f"/api/jobs/{body['job']['id']}", headers=headers)
    assert job_status.status_code == 200
    assert job_status.json()["job"]["status"] == "waiting_for_upload"
    video_upload = next(item for item in body["fileUploads"] if item["kind"] == "video")

    part = client.put(
        f"/api/uploads/{body['uploadId']}/parts/1?fileId={video_upload['fileId']}",
        headers=headers,
        content=b"abcde",
    )
    assert part.status_code == 200, part.text
    assert part.json()["part"]["uploadedBytes"] == 5


def test_initiate_uses_chosen_session_name(tmp_path) -> None:
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    chosen = "OSCE Round 3 - Student A"
    response = client.post(
        "/api/uploads/initiate",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "workflow": "standard",
            "autoProcess": True,
            "sessionName": f"  {chosen}  ",
            "files": [
                {"kind": "video", "originalName": "station.mp4", "mimeType": "video/mp4", "sizeBytes": 5},
                {"kind": "caseStudy", "originalName": "case.pdf", "mimeType": "application/pdf", "sizeBytes": 4},
            ],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["session"]["name"] == chosen


def test_async_upload_complete_commits_local_parts(tmp_path) -> None:
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    async def fake_video_duration(_path: Path) -> float:
        return 1.0

    client.app.state.container.media.get_video_duration_seconds = fake_video_duration
    video_bytes = b"video-bytes"
    case_study_bytes = b"%PDF-1.4 case"
    initiate = client.post(
        "/api/uploads/initiate",
        headers=headers,
        json={
            "workflow": "standard",
            "autoProcess": False,
            "files": [
                {
                    "kind": "video",
                    "originalName": "station.mp4",
                    "mimeType": "video/mp4",
                    "sizeBytes": len(video_bytes),
                },
                {
                    "kind": "caseStudy",
                    "originalName": "case.pdf",
                    "mimeType": "application/pdf",
                    "sizeBytes": len(case_study_bytes),
                },
            ],
        },
    )
    assert initiate.status_code == 201, initiate.text
    body = initiate.json()
    video_upload = next(item for item in body["fileUploads"] if item["kind"] == "video")
    case_upload = next(item for item in body["fileUploads"] if item["kind"] == "caseStudy")

    video_part = client.put(
        f"/api/uploads/{body['uploadId']}/parts/1?fileId={video_upload['fileId']}",
        headers=headers,
        content=video_bytes,
    )
    assert video_part.status_code == 200, video_part.text
    case_part = client.put(
        f"/api/uploads/{body['uploadId']}/parts/1?fileId={case_upload['fileId']}",
        headers=headers,
        content=case_study_bytes,
    )
    assert case_part.status_code == 200, case_part.text

    # Completion is non-blocking: the handler returns 202 immediately with the
    # upload/session in "assembling" while the heavy part-concatenation and
    # validation run in a background asyncio task.
    complete = client.post(
        f"/api/uploads/{body['uploadId']}/complete",
        headers=headers,
        json={},
    )
    assert complete.status_code == 202, complete.text
    complete_body = complete.json()
    assert complete_body["upload"]["status"] == "assembling"
    assert complete_body["session"]["status"] == "assembling"

    # The TestClient tears down the per-request event loop, orphaning the
    # fire-and-forget assembly task, so drive it deterministically here to
    # assert the committed end state.
    container = client.app.state.container
    asyncio.run(container.async_uploads._assemble_and_dispatch(body["uploadId"], False))

    committed_upload = asyncio.run(container.async_uploads.repository.read(body["uploadId"]))
    assert committed_upload["status"] == "committed"
    committed_session = asyncio.run(container.sessions.read(complete_body["session"]["id"]))
    assert committed_session["status"] == "uploaded"
    assert committed_session["files"]["video"]["storageRef"]["status"] == "committed"
    assert committed_session["files"]["caseStudy"]["rubricAssetId"]

    # Re-completing an already-committed upload is idempotent.
    retry = client.post(
        f"/api/uploads/{body['uploadId']}/complete",
        headers=headers,
        json={},
    )
    assert retry.status_code == 202, retry.text
    assert retry.json()["upload"]["status"] == "committed"


def test_get_session_does_not_persist_from_read_path(tmp_path) -> None:
    """GET must generate the subtitle track for the response without writing it
    back to the DB, so a stale read cannot clobber a concurrent worker update."""
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    container = client.app.state.container
    srt_dir = container.settings.paths.output_whisperx_dir / "s-read"
    srt_dir.mkdir(parents=True, exist_ok=True)
    srt_path = srt_dir / "subs.srt"
    srt_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")

    asyncio.run(
        container.sessions.write(
            {
                "id": "s-read",
                "name": "ReadPath",
                "createdAt": "2026-01-01T00:00:00Z",
                "status": "processing",
                "outputs": {"subtitle": {"fileName": "subs.srt", "absolutePath": str(srt_path)}, "videoClips": []},
            }
        )
    )

    response = client.get("/api/sessions/s-read", headers=headers)
    assert response.status_code == 200, response.text
    # The subtitle track is present in the response (functionality preserved)...
    assert response.json()["session"]["outputs"]["subtitleTrack"] is not None
    # ...and the VTT file was generated on disk...
    assert (srt_dir / "subs.vtt").exists()
    # ...but it was NOT persisted (no write from the read path).
    raw = asyncio.run(container.sessions.read("s-read"))
    assert (raw.get("outputs") or {}).get("subtitleTrack") is None
    assert raw["status"] == "processing"


def test_direct_upload_uses_object_storage_refs(tmp_path) -> None:
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    response = client.post(
        "/api/upload",
        headers={"Authorization": f"Bearer {token}"},
        files={
            "video": ("station video.mp4", b"video-bytes", "video/mp4"),
            "caseStudy": ("case.pdf", b"%PDF-1.4 case", "application/pdf"),
        },
    )

    assert response.status_code == 200, response.text
    session = response.json()["session"]
    video = session["files"]["video"]
    case_study = session["files"]["caseStudy"]
    assert video["url"].startswith("/media/source/sessions/")
    assert video["storageRef"]["provider"] == "local"
    assert video["storageRef"]["key"].endswith("/source/video/station-video.mp4")
    assert "localPath" not in video["storageRef"]
    assert case_study["storageRef"]["key"].endswith("/source/caseStudy/case.pdf")

    raw_session = asyncio.run(client.app.state.container.sessions.read(session["id"]))
    raw_video = raw_session["files"]["video"]
    assert raw_video["storageRef"]["localPath"].startswith(str(tmp_path / "storage" / "objects"))
    assert Path(raw_video["absolutePath"]).exists()
