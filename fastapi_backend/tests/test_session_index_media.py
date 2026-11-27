"""The media summary on the session-list projection (thumbnail, duration, Watch)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.database.orm import OrmDatabase
from app.repositories.session_repository import SessionRepository


def project(tmp_path: Path, files: dict[str, Any] | None) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        repository = SessionRepository(OrmDatabase(tmp_path / "s.sqlite3"))
        await repository.database.initialize()
        payload: dict[str, Any] = {"id": "s1", "name": "s1", "status": "completed", "workflow": "standard", "outputs": {}}
        if files is not None:
            payload["files"] = files
        await repository.write(payload)
        return next(row for row in await repository.read_index_projection() if row["id"] == "s1")

    return asyncio.run(_run())


VIDEO = {
    "absolutePath": "/x.mp4",
    "poster": {"url": "/media/playback/s1/s1-poster.jpg"},
    "playback": {"url": "/media/playback/s1/s1-playback.mp4"},
    "media": {
        "status": "running", "stage": "converting", "progress": 0.42, "attempts": 2,
        "durationSeconds": 120.5, "browserPlayable": False, "width": 1920,
    },
}


def test_new_rows_carry_the_media_summary(tmp_path: Path) -> None:
    row = project(tmp_path, {"video": VIDEO})
    assert row["media"] == {
        "status": "running", "stage": "converting", "progress": 0.42, "attempts": 2,
        "durationSeconds": 120.5, "browserPlayable": False,
    }
    assert row["posterUrl"] == "/media/playback/s1/s1-poster.jpg"
    assert row["hasPlayback"] is True


def test_old_rows_project_nulls(tmp_path: Path) -> None:
    row = project(tmp_path, {"video": {"absolutePath": "/x.mp4"}})
    assert row["media"] is None and row["posterUrl"] is None and row["hasPlayback"] is False


def test_row_without_files_projects_nulls(tmp_path: Path) -> None:
    row = project(tmp_path, None)
    assert row["media"] is None and row["posterUrl"] is None and row["hasPlayback"] is False


def test_purged_video_offers_no_media_poster_or_playback(tmp_path: Path) -> None:
    row = project(tmp_path, {"video": {**VIDEO, "purgedAt": "2026-01-01T00:00:00Z"}})
    assert row["posterUrl"] is None
    assert row["hasPlayback"] is False
    assert row["media"] is None


def test_ready_media_without_progress(tmp_path: Path) -> None:
    media = {"status": "ready", "durationSeconds": 10, "browserPlayable": True}
    row = project(tmp_path, {"video": {"absolutePath": "/x", "media": media}})
    assert row["media"] == {
        "status": "ready", "stage": None, "progress": None, "attempts": None,
        "durationSeconds": 10.0, "browserPlayable": True,
    }
