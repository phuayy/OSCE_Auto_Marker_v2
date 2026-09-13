from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app.api.dependencies import authorize_request
from app.api.routes import (
    async_uploads,
    auth,
    events as events_routes,
    health,
    jobs,
    notifications,
    sessions,
    settings as settings_routes,
    webhooks,
)
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

    # Production maps a schema rejection to 400 + {"error": ...}; without the
    # same handler here the tests would assert FastAPI's default 422 shape and
    # stop reflecting what clients actually receive.
    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        first_error = exc.errors()[0] if exc.errors() else {}
        message = str(first_error.get("msg") or "Invalid request payload.")
        return JSONResponse(status_code=400, content={"error": message})

    app.mount(
        "/media/scores",
        StaticFiles(directory=str(settings.paths.output_scores_dir), check_dir=False),
        name="media-scores",
    )
    app.include_router(health.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(async_uploads.router, prefix="/api")
    app.include_router(jobs.router, prefix="/api")
    app.include_router(sessions.router, prefix="/api")
    app.include_router(notifications.router, prefix="/api")
    app.include_router(events_routes.router, prefix="/api")
    app.include_router(webhooks.router, prefix="/api")
    app.include_router(settings_routes.router, prefix="/api")
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


def test_notifications_list_and_mark_read(tmp_path) -> None:
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    container = client.app.state.container

    asyncio.run(container.notifications.notify("Scoring complete", "Session A is scored.", session_id="s1"))

    listed = client.get("/api/notifications", headers=headers)
    assert listed.status_code == 200
    body = listed.json()
    assert body["unreadCount"] == 1
    assert body["notifications"][0]["title"] == "Scoring complete"

    notification_id = body["notifications"][0]["id"]
    marked = client.post(f"/api/notifications/{notification_id}/read", headers=headers)
    assert marked.status_code == 200
    assert marked.json()["unreadCount"] == 0

    missing = client.post("/api/notifications/nope/read", headers=headers)
    assert missing.status_code == 404


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


def test_initiate_snapshots_selected_corpus_into_session(tmp_path) -> None:
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    container = client.app.state.container
    corpus = asyncio.run(container.corpora.create("Common Cold", ["nasal block", "paracetamol"]))

    files = [
        {"kind": "video", "originalName": "station.mp4", "mimeType": "video/mp4", "sizeBytes": 5},
        {"kind": "caseStudy", "originalName": "case.pdf", "mimeType": "application/pdf", "sizeBytes": 4},
    ]
    response = client.post(
        "/api/uploads/initiate",
        headers=headers,
        json={"workflow": "standard", "autoProcess": True, "corpusId": corpus["id"], "files": files},
    )
    assert response.status_code == 201, response.text
    public = response.json()["session"]["corpus"]
    assert public == {"id": corpus["id"], "name": "Common Cold", "terms": ["nasal block", "paracetamol"]}

    # The snapshot is persisted on the session, so a later corpus edit/delete
    # cannot affect this session's transcription.
    stored = asyncio.run(container.sessions.read(response.json()["session"]["id"]))
    assert stored["corpus"]["terms"] == ["nasal block", "paracetamol"]

    # "None" option: no corpusId (or the literal "none") means no biasing.
    none_response = client.post(
        "/api/uploads/initiate",
        headers=headers,
        json={"workflow": "standard", "autoProcess": True, "corpusId": "none", "files": files},
    )
    assert none_response.status_code == 201, none_response.text
    assert none_response.json()["session"]["corpus"] is None

    unknown = client.post(
        "/api/uploads/initiate",
        headers=headers,
        json={"workflow": "standard", "autoProcess": True, "corpusId": "missing-id", "files": files},
    )
    assert unknown.status_code == 400


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


def test_failed_assembly_marks_upload_failed_and_allows_retry(tmp_path) -> None:
    """A crashed background assembly must not leave the upload stuck on
    "assembling" (which would make every /complete retry a no-op forever)."""
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    container = client.app.state.container

    async def fake_video_duration(_path: Path) -> float:
        return 1.0

    container.media.get_video_duration_seconds = fake_video_duration
    video_bytes = b"video-bytes"
    case_study_bytes = b"%PDF-1.4 case"
    initiate = client.post(
        "/api/uploads/initiate",
        headers=headers,
        json={
            "workflow": "standard",
            "autoProcess": False,
            "files": [
                {"kind": "video", "originalName": "station.mp4", "mimeType": "video/mp4", "sizeBytes": len(video_bytes)},
                {"kind": "caseStudy", "originalName": "case.pdf", "mimeType": "application/pdf", "sizeBytes": len(case_study_bytes)},
            ],
        },
    )
    assert initiate.status_code == 201, initiate.text
    body = initiate.json()
    for kind, payload in (("video", video_bytes), ("caseStudy", case_study_bytes)):
        plan = next(item for item in body["fileUploads"] if item["kind"] == kind)
        part = client.put(
            f"/api/uploads/{body['uploadId']}/parts/1?fileId={plan['fileId']}",
            headers=headers,
            content=payload,
        )
        assert part.status_code == 200, part.text

    complete = client.post(f"/api/uploads/{body['uploadId']}/complete", headers=headers, json={})
    assert complete.status_code == 202, complete.text
    session_id = complete.json()["session"]["id"]

    # Make assembly blow up, then drive the orphaned background task directly.
    original_complete_file = container.async_uploads.storage.complete_file

    async def boom(_upload, _file_record):
        raise RuntimeError("disk full")

    container.async_uploads.storage.complete_file = boom
    asyncio.run(container.async_uploads._assemble_and_dispatch(body["uploadId"], False))

    failed_upload = asyncio.run(container.async_uploads.repository.read(body["uploadId"]))
    assert failed_upload["status"] == "failed"
    assert "disk full" in str(failed_upload.get("error"))
    failed_session = asyncio.run(container.sessions.read(session_id))
    assert failed_session["status"] == "failed"

    # A retry is accepted (not short-circuited by the idempotency branches) and
    # succeeds once the underlying fault is gone.
    container.async_uploads.storage.complete_file = original_complete_file
    retry = client.post(f"/api/uploads/{body['uploadId']}/complete", headers=headers, json={})
    assert retry.status_code == 202, retry.text
    assert retry.json()["upload"]["status"] == "assembling"
    asyncio.run(container.async_uploads._assemble_and_dispatch(body["uploadId"], False))
    assert asyncio.run(container.async_uploads.repository.read(body["uploadId"]))["status"] == "committed"
    assert asyncio.run(container.sessions.read(session_id))["status"] == "uploaded"


def test_single_shot_upload_route_is_gone(tmp_path) -> None:
    """``POST /api/upload`` was a second, separately-validated ingest path that
    no client used and that produced sessions the UI could not start. It is
    removed; this pins that it stays removed, because reintroducing it means
    reintroducing two places where an upload's invariants are enforced."""
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    response = client.post(
        "/api/upload",
        headers={"Authorization": f"Bearer {token}"},
        files={
            "video": ("station.mp4", b"video-bytes", "video/mp4"),
            "caseStudy": ("case.pdf", b"%PDF-1.4 case", "application/pdf"),
        },
    )
    assert response.status_code == 404, response.text


def _chunked_upload(
    client: TestClient,
    headers: dict[str, str],
    *,
    video_name: str = "station.mp4",
    case_study_name: str = "case.pdf",
    **metadata: object,
) -> dict:
    """Run one whole chunked upload (initiate -> parts -> complete -> assemble)
    and return the committed raw session."""

    async def fake_video_duration(_path: Path) -> float:
        return 1.0

    container = client.app.state.container
    container.media.get_video_duration_seconds = fake_video_duration

    video_bytes = b"video-bytes"
    case_study_bytes = b"%PDF-1.4 case"
    initiate = client.post(
        "/api/uploads/initiate",
        headers=headers,
        json={
            "autoProcess": False,
            "files": [
                {
                    "kind": "video",
                    "originalName": video_name,
                    "mimeType": "video/mp4",
                    "sizeBytes": len(video_bytes),
                },
                {
                    "kind": "caseStudy",
                    "originalName": case_study_name,
                    "mimeType": "application/pdf",
                    "sizeBytes": len(case_study_bytes),
                },
            ],
            **metadata,
        },
    )
    assert initiate.status_code == 201, initiate.text
    body = initiate.json()
    for kind, payload in (("video", video_bytes), ("caseStudy", case_study_bytes)):
        file_upload = next(item for item in body["fileUploads"] if item["kind"] == kind)
        part = client.put(
            f"/api/uploads/{body['uploadId']}/parts/1?fileId={file_upload['fileId']}",
            headers=headers,
            content=payload,
        )
        assert part.status_code == 200, part.text
    complete = client.post(f"/api/uploads/{body['uploadId']}/complete", headers=headers, json={})
    assert complete.status_code == 202, complete.text
    asyncio.run(container.async_uploads._assemble_and_dispatch(body["uploadId"], False))
    return asyncio.run(container.sessions.read(body["session"]["id"]))


def test_chunked_upload_persists_workflow_segmentation_and_name(tmp_path) -> None:
    """The invariants the removed legacy route enforced separately — a trimmed
    name, a known workflow, the "human" segmentation synonym — are enforced once
    now, by the shared ``UploadMetadataMixin`` on the only ingest path."""
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    session = _chunked_upload(
        client,
        {"Authorization": f"Bearer {token}"},
        workflow="long",
        segmentation="human",
        sessionName="  Cohort A  ",
    )
    assert session["workflow"] == "long"
    assert session["segmentation"] == "person"  # "human" synonym normalized
    assert session["name"] == "Cohort A"


def test_upload_uses_object_storage_refs(tmp_path) -> None:
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    raw_session = _chunked_upload(
        client,
        {"Authorization": f"Bearer {token}"},
        video_name="station video.mp4",
    )

    public = client.app.state.container.sessions.public_session(raw_session)
    video = public["files"]["video"]
    case_study = public["files"]["caseStudy"]
    assert video["url"].startswith("/media/source/sessions/")
    assert video["storageRef"]["provider"] == "local"
    assert video["storageRef"]["key"].endswith("/source/video/station-video.mp4")
    # Server paths never leave the process through a public projection.
    assert "localPath" not in video["storageRef"]
    assert case_study["storageRef"]["key"].endswith("/source/caseStudy/case.pdf")

    raw_video = raw_session["files"]["video"]
    assert raw_video["storageRef"]["localPath"].startswith(str(tmp_path / "storage" / "objects"))
    assert Path(raw_video["absolutePath"]).exists()


def _initiate_with_one_part(client: TestClient, headers: dict[str, str]) -> str:
    """Initiate an upload and push a single video part; return the upload id."""
    video_bytes = b"video-bytes"
    initiate = client.post(
        "/api/uploads/initiate",
        headers=headers,
        json={
            "workflow": "standard",
            "autoProcess": False,
            "files": [
                {"kind": "video", "originalName": "s.mp4", "mimeType": "video/mp4", "sizeBytes": len(video_bytes)},
                {"kind": "caseStudy", "originalName": "c.pdf", "mimeType": "application/pdf", "sizeBytes": 5},
            ],
        },
    )
    assert initiate.status_code == 201, initiate.text
    body = initiate.json()
    video = next(item for item in body["fileUploads"] if item["kind"] == "video")
    part = client.put(
        f"/api/uploads/{body['uploadId']}/parts/1?fileId={video['fileId']}",
        headers=headers,
        content=video_bytes,
    )
    assert part.status_code == 200, part.text
    return body["uploadId"]


def test_recover_expired_uploads_reclaims_abandoned_and_spares_fresh(tmp_path) -> None:
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    container = client.app.state.container
    service = container.async_uploads

    expired_id = _initiate_with_one_part(client, headers)
    fresh_id = _initiate_with_one_part(client, headers)

    # Backdate the first upload past its TTL; leave the second inside its window.
    expired_upload = asyncio.run(service.repository.read(expired_id))
    expired_upload["expiresAt"] = "2000-01-01T00:00:00Z"
    asyncio.run(service.repository.write(expired_upload))

    parts_dir = container.storage.settings.object_storage_root / ".uploads" / expired_id
    assert parts_dir.exists()  # raw part files present before the sweep

    asyncio.run(service.recover_expired_uploads())

    # Expired upload fully reclaimed.
    reclaimed = asyncio.run(service.repository.read(expired_id))
    assert reclaimed["status"] == "expired"
    assert "expiredAt" in reclaimed
    assert not parts_dir.exists()  # 500 MB-equivalent leak freed

    expired_session = asyncio.run(container.sessions.read(str(expired_upload["sessionId"])))
    assert expired_session["status"] == "failed"
    assert "expired" in (expired_session.get("error") or "").lower()

    expired_job = asyncio.run(container.jobs.repository.read(str(expired_upload["jobId"])))
    assert expired_job["status"] == "cancelled"

    # Fresh upload untouched — still live, parts intact.
    fresh_upload = asyncio.run(service.repository.read(fresh_id))
    assert fresh_upload["status"] == "uploading"
    fresh_parts = container.storage.settings.object_storage_root / ".uploads" / fresh_id
    assert fresh_parts.exists()
    fresh_session = asyncio.run(container.sessions.read(str(fresh_upload["sessionId"])))
    assert fresh_session["status"] == "waiting_for_upload"


def test_recrop_answers_202_with_a_queued_job(tmp_path) -> None:
    """Re-cutting a clip is queued work, not work done in the request.

    The handler used to run ffmpeg inline and answer 200 with the finished
    clip; it now answers 202 with the clip as a draft and the export job that
    will cut it.
    """
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    container = client.app.state.container

    video_path = tmp_path / "recording.mp4"
    video_path.write_bytes(b"video")
    clip_path = tmp_path / "clip-1.mp4"
    clip_path.write_bytes(b"clip")
    asyncio.run(
        container.sessions.write(
            {
                "id": "s-recrop",
                "name": "Long Station",
                "createdAt": "2026-01-01T00:00:00Z",
                "status": "cropped",
                "workflow": "long",
                "files": {"video": {"absolutePath": str(video_path)}},
                "outputs": {
                    "videoClips": [
                        {
                            "id": "clip-a",
                            "label": "Student A",
                            "kind": "session",
                            "start": 0.0,
                            "end": 120.0,
                            "exportIndex": 0,
                            "planId": "plan-1",
                            "fileName": "clip-1.mp4",
                            "absolutePath": str(clip_path),
                            "url": "/media/clips/s-recrop/plan-1/clip-1.mp4",
                            "isDraft": False,
                        }
                    ]
                },
            }
        )
    )

    async def fake_video_duration(_path: Path) -> float:
        return 600.0

    container.media.get_video_duration_seconds = fake_video_duration

    response = client.post(
        "/api/sessions/s-recrop/clips/clip-a/recrop",
        headers=headers,
        json={"start": 10.0, "end": 140.0},
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["job"]["taskType"] == "export_clips"
    assert body["clipExport"]["scope"] == "clip"
    assert body["clipExport"]["clipIds"] == ["clip-a"]
    # The clip comes back as a draft: its new range exists, its MP4 does not yet.
    assert body["clip"]["isDraft"] is True
    assert body["clip"]["revision"] == 1
    assert (body["clip"]["start"], body["clip"]["end"]) == (10.0, 140.0)
