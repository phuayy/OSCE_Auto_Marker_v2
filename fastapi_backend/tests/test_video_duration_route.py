"""GET /api/sessions/{id}/video-duration — the server-side fallback the clip
timeline uses when the browser cannot decode the source (HEVC, mu-law)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from tests.test_routes import build_test_client


def _signed_in(tmp_path: Path):
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    return client, {"Authorization": f"Bearer {token}"}


def _seed(client, session_id: str, video: dict | None) -> None:
    document = {
        "id": session_id,
        "name": "Fixture",
        "createdAt": "2026-01-01T00:00:00Z",
        "status": "cropped",
        "outputs": {"videoClips": []},
    }
    if video is not None:
        document["files"] = {"video": video}
    asyncio.run(client.app.state.container.sessions.write(document))


def test_duration_is_probed_from_the_source_file(tmp_path) -> None:
    client, headers = _signed_in(tmp_path)
    video_path = tmp_path / "source.mp4"
    video_path.write_bytes(b"hevc-bytes")
    probed: list[Path] = []

    async def fake_duration(path: Path) -> float:
        probed.append(path)
        return 1834.5

    client.app.state.container.media.get_video_duration_seconds = fake_duration
    _seed(client, "s-dur", {"absolutePath": str(video_path)})

    response = client.get("/api/sessions/s-dur/video-duration", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"durationSeconds": 1834.5}
    assert probed == [video_path]


def test_recorded_duration_skips_ffprobe(tmp_path) -> None:
    client, headers = _signed_in(tmp_path)

    async def must_not_probe(_path: Path) -> float:
        raise AssertionError("ffprobe should not run when the duration is recorded")

    client.app.state.container.media.get_video_duration_seconds = must_not_probe
    _seed(client, "s-rec", {"absolutePath": str(tmp_path / "gone.mp4"), "media": {"durationSeconds": 600}})

    response = client.get("/api/sessions/s-rec/video-duration", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"durationSeconds": 600.0}


def test_missing_video_is_a_400_and_unknown_session_a_404(tmp_path) -> None:
    client, headers = _signed_in(tmp_path)
    _seed(client, "s-none", {"absolutePath": str(tmp_path / "missing.mp4")})

    assert client.get("/api/sessions/s-none/video-duration", headers=headers).status_code == 400
    assert client.get("/api/sessions/nope/video-duration", headers=headers).status_code == 404


def test_unreadable_video_is_a_422(tmp_path) -> None:
    client, headers = _signed_in(tmp_path)
    video_path = tmp_path / "broken.mp4"
    video_path.write_bytes(b"not-a-video")

    async def failing_duration(_path: Path) -> float:
        raise RuntimeError("Could not determine video duration")

    client.app.state.container.media.get_video_duration_seconds = failing_duration
    _seed(client, "s-bad", {"absolutePath": str(video_path)})

    assert client.get("/api/sessions/s-bad/video-duration", headers=headers).status_code == 422
