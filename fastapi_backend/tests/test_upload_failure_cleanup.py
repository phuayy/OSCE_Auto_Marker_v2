"""Audit M27: an assembly that fails after its objects were committed removes them."""

from __future__ import annotations

import asyncio

from app.core.exceptions import AppError
from app.schemas.uploads import CompleteUploadRequest
from app.services.container import create_container
from tests.test_session_maintenance import _init, _settings
from tests.fixtures.local_upload import PDF, committed_files, local_upload, settled, stub_ffprobe


def test_a_non_pdf_case_study_leaves_no_object_behind(tmp_path) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        await _init(container)
        stub_ffprobe(container)
        upload_id, session_id = await local_upload(container, b"plain text named pdf")
        await container.async_uploads.complete(upload_id, CompleteUploadRequest())
        session = await settled(container, session_id)

        assert session["status"] == "failed"
        assert committed_files(container) == []
        await container.shutdown()

    asyncio.run(_run())


def test_an_unreadable_video_fails_the_session_and_removes_the_committed_objects(tmp_path) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        await _init(container)
        stub_ffprobe(container, fail=True)
        upload_id, session_id = await local_upload(container, PDF)
        await container.async_uploads.complete(upload_id, CompleteUploadRequest())
        session = await settled(container, session_id)

        assert session["status"] == "failed"
        assert committed_files(container) == []
        await container.shutdown()

    asyncio.run(_run())


def test_a_failed_cleanup_is_logged_and_does_not_mask_the_original_error(tmp_path, caplog) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        await _init(container)
        stub_ffprobe(container, fail=True)

        async def broken(_ref):
            raise OSError("disk went away")

        container.storage.delete_committed_object = broken
        upload_id, session_id = await local_upload(container, PDF)
        await container.async_uploads.complete(upload_id, CompleteUploadRequest())
        session = await settled(container, session_id)

        assert session["status"] == "failed"
        assert "could not be read" in session["error"]
        await container.shutdown()

    with caplog.at_level("WARNING"):
        asyncio.run(_run())
    assert any("committed object" in record.getMessage() for record in caplog.records)


def test_the_registered_case_study_asset_is_not_deleted_by_a_later_failure(tmp_path) -> None:
    """Once the case study is a shared rubric asset it is not this upload's to
    delete, so a failure after registration removes only the video."""

    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        await _init(container)
        stub_ffprobe(container)

        async def boom(*_args, **_kwargs):
            raise AppError("commit exploded", status_code=500)

        container.async_uploads._commit_assembled = boom
        upload_id, session_id = await local_upload(container, PDF)
        await container.async_uploads.complete(upload_id, CompleteUploadRequest())
        session = await settled(container, session_id)

        assert session["status"] == "failed"
        remaining = committed_files(container)
        assert [p.suffix for p in remaining] == [".pdf"]
        await container.shutdown()

    asyncio.run(_run())




def test_gcs_failure_removes_the_bucket_objects_through_the_same_call(tmp_path, monkeypatch) -> None:
    from tests.test_gcs_upload_lifecycle import _gcs_container, _init_gcs, _upload_directly, _until_settled

    async def _run() -> None:
        container, bucket = _gcs_container(tmp_path, monkeypatch)
        await _init_gcs(container)

        async def unreadable(_ref):
            raise AppError("Uploaded video could not be read.", status_code=400)

        monkeypatch.setattr(container.async_uploads, "_validate_committed_video", unreadable)
        upload_id, session_id, keys = await _upload_directly(container, bucket)
        await container.async_uploads.complete(upload_id, CompleteUploadRequest())
        session = await _until_settled(container, session_id)

        assert session["status"] == "failed"
        assert keys["video"] not in bucket.objects
        assert keys["caseStudy"] not in bucket.objects
        await container.shutdown()

    asyncio.run(_run())
