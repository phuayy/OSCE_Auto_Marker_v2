"""Audit M26: a signed playback URL is a read-time capability, resolved from the
stored storage ref when a session is served, never a persisted string."""

from __future__ import annotations

import asyncio

from app.core.exceptions import AppError
from app.schemas.uploads import CompleteUploadRequest
from app.services.container import create_container
from tests.test_session_maintenance import _init, _settings
from tests.fixtures.local_upload import PDF, local_upload, settled, stub_ffprobe


# --- M26: playback URL is resolved at read time -------------------------------


def test_local_playback_url_is_the_media_path(tmp_path) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        ref = {"provider": "local", "key": "sessions/s1/video/a.mp4"}
        assert container.storage.playback_url(ref) == "/media/source/sessions/s1/video/a.mp4"
        assert container.storage.playback_url({"provider": "gcs", "key": "k"}) is None
        await container.shutdown()

    asyncio.run(_run())


def test_public_session_signs_a_fresh_url_on_every_read(tmp_path) -> None:
    async def _run() -> None:
        from dataclasses import replace

        container = create_container(
            replace(_settings(tmp_path), storage_backend="gcs", gcs_bucket="osce-test")
        )
        signed: list[str] = []

        def sign(key: str) -> str:
            signed.append(key)
            return f"https://signed.example.invalid/{key}?n={len(signed)}"

        container.storage.public_url_for_key = sign
        session = {
            "id": "s1",
            "files": {
                "video": {
                    "url": "https://signed.example.invalid/stale?expired=1",  # legacy stored URL
                    "storageRef": {"provider": "gcs", "bucket": "osce-test", "key": "sessions/s1/v.mp4"},
                }
            },
        }
        first = container.sessions.public_session(session)["files"]["video"]["url"]
        second = container.sessions.public_session(session)["files"]["video"]["url"]
        assert first.endswith("n=1") and second.endswith("n=2")
        assert "stale" not in first
        await container.shutdown()

    asyncio.run(_run())


def test_a_signing_failure_falls_back_to_the_stored_url_instead_of_failing_the_read(tmp_path) -> None:
    async def _run() -> None:
        from dataclasses import replace

        container = create_container(
            replace(_settings(tmp_path), storage_backend="gcs", gcs_bucket="osce-test")
        )

        def sign(_key: str) -> str:
            raise AppError("cannot sign", status_code=500)

        container.storage.public_url_for_key = sign
        session = {
            "id": "s1",
            "files": {"video": {"url": "stored", "storageRef": {"provider": "gcs", "key": "k"}}},
        }
        assert container.sessions.public_session(session)["files"]["video"]["url"] == "stored"
        await container.shutdown()

    asyncio.run(_run())


def test_a_session_without_a_storage_ref_keeps_its_stored_url(tmp_path) -> None:
    """Clip children point at their parent's exported MP4 under /media/clips."""

    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        session = {"id": "c1", "files": {"video": {"url": "/media/clips/p/clip-1.mp4"}}}
        assert container.sessions.public_session(session)["files"]["video"]["url"] == "/media/clips/p/clip-1.mp4"
        await container.shutdown()

    asyncio.run(_run())


def test_a_purged_video_has_no_url_even_though_its_ref_remains(tmp_path) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        session = {
            "id": "s1",
            "files": {
                "video": {
                    "url": None,
                    "purgedAt": "2026-01-01T00:00:00+00:00",
                    "storageRef": {"provider": "local", "key": "sessions/s1/video/a.mp4"},
                }
            },
        }
        assert container.sessions.public_session(session)["files"]["video"]["url"] is None
        await container.shutdown()

    asyncio.run(_run())


def test_a_committed_session_no_longer_persists_a_video_url(tmp_path) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        await _init(container)
        stub_ffprobe(container)
        upload_id, session_id = await local_upload(container, PDF)
        await container.async_uploads.complete(upload_id, CompleteUploadRequest())
        session = await settled(container, session_id)
        assert "url" not in session["files"]["video"]
        public = container.sessions.public_session(session)
        assert public["files"]["video"]["url"].startswith("/media/source/")
        await container.shutdown()

    asyncio.run(_run())
