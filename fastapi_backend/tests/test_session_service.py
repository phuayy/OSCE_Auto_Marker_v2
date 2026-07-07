from __future__ import annotations

from app.core.config import Settings
from app.database.orm import OrmDatabase
from app.repositories.session_repository import SessionRepository
from app.services.session_service import SessionService


def _make_service(tmp_path) -> SessionService:
    settings = Settings(root_dir=tmp_path, backend_root=tmp_path, ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", scorer_python_bin="python")
    repo = SessionRepository(OrmDatabase(tmp_path / "sessions.sqlite3"))
    return SessionService(settings, repo)


def test_public_session_hides_absolute_paths(tmp_path) -> None:
    service = _make_service(tmp_path)
    public = service.public_session(
        {
            "id": "s1",
            "name": "Case 1",
            "createdAt": "2026-01-01T00:00:00Z",
            "status": "uploaded",
            "pipeline": {},
            "files": {
                "video": {
                    "originalName": "video.mp4",
                    "fileName": "s1-video.mp4",
                    "absolutePath": "C:/secret/video.mp4",
                    "url": "/media/videos/s1-video.mp4",
                    "sizeBytes": 10,
                },
                "caseStudy": None,
            },
            "outputs": {"audio": None, "videoClips": None},
            "error": None,
        }
    )
    assert "absolutePath" not in str(public)
    assert public["files"]["video"]["url"] == "/media/videos/s1-video.mp4"


def test_reserve_unique_session_name_handles_conflicts(tmp_path) -> None:
    service = _make_service(tmp_path)
    used = {"case"}
    assert service.reserve_unique_session_name(used, "Case") == "Case 2"
    assert "case 2" in used
