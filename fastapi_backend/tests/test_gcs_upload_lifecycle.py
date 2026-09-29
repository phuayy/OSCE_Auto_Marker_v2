"""Audit 2026-09-29 findings 3 and 4: the GCS backend end to end.

initiate -> the browser PUTs straight to the bucket -> complete -> background
commit, against an in-memory bucket double (no credentials, no network). The
storage adapter was tested alone; the flow through ``AsyncUploadService`` was
not, and it was broken three ways:

* ``complete`` demanded relayed parts, which a direct upload never has;
* the post-commit cleanup called ``abort_upload``, which on GCS deletes the
  very objects just committed;
* retention and deletion unlinked only the local cache, leaving the recording
  in the bucket while the session said it was gone.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path

from google.api_core.exceptions import NotFound

from app.schemas.uploads import CompleteUploadRequest, InitiateUploadRequest
from app.services.container import create_container
from app.storage import GcsObjectStorageService
from tests.test_session_maintenance import _init, _settings

VIDEO = b"v" * 64
CASE_STUDY = b"%PDF-1.4 fake case study"


class FakeBlob:
    _next_generation = 1000

    def __init__(self, bucket: "FakeBucket", name: str) -> None:
        self.bucket = bucket
        self.name = name
        self.data = b""
        self.content_type = None
        self.generation = None

    @property
    def size(self) -> int:
        return len(self.data)

    def create_resumable_upload_session(self, content_type=None, size=None, origin=None) -> str:
        return f"https://storage.example.invalid/upload/{self.name}"

    def download_to_file(self, target) -> None:
        target.write(self.data)


class FakeBucket:
    def __init__(self) -> None:
        self.objects: dict[str, FakeBlob] = {}
        self.deleted: list[tuple[str, object]] = []

    def exists(self) -> bool:
        return True

    def blob(self, name: str) -> FakeBlob:
        return self.objects.get(name) or FakeBlob(self, name)

    def get_blob(self, name: str):
        return self.objects.get(name)

    def delete_blob(self, name: str, if_generation_match=None, **_kwargs) -> None:
        if name not in self.objects:
            raise NotFound(f"no such object: {name}")
        self.deleted.append((name, if_generation_match))
        del self.objects[name]

    # What the browser does with the resumable session URI.
    def receive_direct_upload(self, name: str, data: bytes, content_type: str) -> None:
        blob = FakeBlob(self, name)
        blob.data = data
        blob.content_type = content_type
        FakeBlob._next_generation += 1
        blob.generation = FakeBlob._next_generation
        self.objects[name] = blob


def _gcs_container(tmp_path: Path, monkeypatch, **overrides):
    settings = replace(
        _settings(tmp_path),
        storage_backend="gcs",
        gcs_bucket="osce-test",
        gcs_cache_root_override=str(tmp_path / "gcs-cache"),
        **overrides,
    )
    container = create_container(settings)
    assert isinstance(container.storage, GcsObjectStorageService)
    bucket = FakeBucket()
    container.storage._bucket = bucket

    async def _skip(*_args, **_kwargs):
        return None

    # ffprobe and the PDF parser are not what this file is about.
    monkeypatch.setattr(container.async_uploads, "_validate_committed_video", _skip)
    monkeypatch.setattr(container.async_uploads, "_validate_committed_case_study", _skip)

    # `public_url_for_key` builds its own real `storage.Client()` rather than
    # going through the faked bucket, because generate_signed_url needs a
    # signing-capable credential that has nothing to do with the bucket
    # contents this test is exercising. Without a fake here it reaches for
    # real Application Default Credentials and fails in any environment
    # (like this one) that has none — not what this file is testing.
    monkeypatch.setattr(
        container.storage, "public_url_for_key", lambda key: f"https://storage.example.invalid/{key}"
    )
    return container, bucket


async def _upload_directly(container, bucket: FakeBucket) -> tuple[str, str, dict]:
    initiated = await container.async_uploads.initiate(
        InitiateUploadRequest(
            files=[
                {"kind": "video", "originalName": "station.mp4", "mimeType": "video/mp4",
                 "sizeBytes": len(VIDEO), "sha256": hashlib.sha256(VIDEO).hexdigest()},
                {"kind": "caseStudy", "originalName": "case.pdf", "mimeType": "application/pdf",
                 "sizeBytes": len(CASE_STUDY)},
            ],
            autoProcess=False,
        )
    )
    upload_id = initiated["uploadId"]
    session_id = initiated["session"]["id"]
    upload = await container.async_uploads.repository.read(upload_id)
    keys = {record["kind"]: record["key"] for record in upload["files"]}
    bucket.receive_direct_upload(keys["video"], VIDEO, "video/mp4")
    bucket.receive_direct_upload(keys["caseStudy"], CASE_STUDY, "application/pdf")
    return upload_id, session_id, keys


async def _until_settled(container, session_id: str) -> dict:
    for _ in range(200):
        session = await container.sessions.read(session_id)
        if session["status"] in {"uploaded", "queued", "failed"}:
            return session
        await asyncio.sleep(0.02)
    raise AssertionError(f"upload never settled: {session['status']}")


def test_a_direct_upload_completes_and_its_objects_survive_the_commit(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        container, bucket = _gcs_container(tmp_path, monkeypatch)
        await _init_gcs(container)
        upload_id, session_id, keys = await _upload_directly(container, bucket)

        # Finding 3: no relayed parts exist, and that must not be a refusal.
        await container.async_uploads.complete(upload_id, CompleteUploadRequest())
        session = await _until_settled(container, session_id)

        assert session["status"] == "uploaded", session.get("error")
        # The post-commit cleanup must release staging, not delete the commit.
        assert keys["video"] in bucket.objects
        assert keys["caseStudy"] in bucket.objects
        assert session["files"]["video"]["storageRef"]["key"] == keys["video"]
        await container.shutdown()

    asyncio.run(_run())


def test_complete_still_refuses_an_object_the_bucket_never_received(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        container, bucket = _gcs_container(tmp_path, monkeypatch)
        await _init_gcs(container)
        upload_id, session_id, keys = await _upload_directly(container, bucket)
        del bucket.objects[keys["video"]]

        try:
            await container.async_uploads.complete(upload_id, CompleteUploadRequest())
        except Exception as error:  # refused up front: fine
            assert getattr(error, "status_code", None) == 400
        else:  # or accepted and failed during commit: also fine, but never "uploaded"
            session = await _until_settled(container, session_id)
            assert session["status"] == "failed"
        await container.shutdown()

    asyncio.run(_run())


def test_startup_recovery_resumes_a_direct_upload_instead_of_failing_it(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        container, bucket = _gcs_container(tmp_path, monkeypatch)
        await _init_gcs(container)
        upload_id, session_id, _keys = await _upload_directly(container, bucket)
        # Simulate a restart between "assembling" and the commit.
        upload = await container.async_uploads.repository.read(upload_id)
        upload["status"] = "assembling"
        upload["autoProcess"] = False
        await container.async_uploads.repository.write(upload)

        await container.async_uploads.recover_stale_assembling_uploads()
        session = await _until_settled(container, session_id)
        assert session["status"] == "uploaded", session.get("error")
        await container.shutdown()

    asyncio.run(_run())


def test_retention_deletes_the_bucket_object_and_records_it_only_after(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        container, bucket = _gcs_container(tmp_path, monkeypatch, session_video_retention_days=1)
        await _init_gcs(container)
        upload_id, session_id, keys = await _upload_directly(container, bucket)
        await container.async_uploads.complete(upload_id, CompleteUploadRequest())
        await _until_settled(container, session_id)
        generation = bucket.objects[keys["video"]].generation

        assert await container.session_maintenance.purge_expired_video(session_id) is True
        assert keys["video"] not in bucket.objects
        # Generation-scoped: only the object this session committed.
        assert (keys["video"], generation) in bucket.deleted or (keys["video"], str(generation)) in bucket.deleted
        # The case study is a shared rubric asset and is never purged here.
        assert keys["caseStudy"] in bucket.objects
        session = await container.sessions.read(session_id)
        assert session["files"]["video"]["purgedAt"]
        await container.shutdown()

    asyncio.run(_run())


def test_a_failed_bucket_delete_is_not_recorded_as_purged(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        container, bucket = _gcs_container(tmp_path, monkeypatch, session_video_retention_days=1)
        await _init_gcs(container)
        upload_id, session_id, keys = await _upload_directly(container, bucket)
        await container.async_uploads.complete(upload_id, CompleteUploadRequest())
        await _until_settled(container, session_id)

        def _refuse(name, if_generation_match=None, **_kwargs):
            raise RuntimeError("simulated bucket outage")

        bucket.delete_blob = _refuse
        try:
            await container.session_maintenance.purge_expired_video(session_id)
        except Exception:
            pass
        session = await container.sessions.read(session_id)
        assert not session["files"]["video"].get("purgedAt")
        assert keys["video"] in bucket.objects
        await container.shutdown()

    asyncio.run(_run())


def test_deleting_the_session_deletes_its_video_object_but_not_the_case_study(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        container, bucket = _gcs_container(tmp_path, monkeypatch)
        await _init_gcs(container)
        upload_id, session_id, keys = await _upload_directly(container, bucket)
        await container.async_uploads.complete(upload_id, CompleteUploadRequest())
        await _until_settled(container, session_id)

        await container.session_maintenance.delete_session(session_id)
        assert keys["video"] not in bucket.objects
        assert keys["caseStudy"] in bucket.objects
        await container.shutdown()

    asyncio.run(_run())


def test_the_expired_upload_sweep_never_deletes_a_committed_upload_objects(tmp_path, monkeypatch) -> None:
    """A committed upload record past its TTL is only bookkeeping: its objects
    are the session's source files now. The sweep used to ``abort_upload`` it,
    which on GCS deletes the bucket objects by the same keys the session reads."""

    async def _run() -> None:
        container, bucket = _gcs_container(tmp_path, monkeypatch)
        await _init_gcs(container)
        upload_id, session_id, keys = await _upload_directly(container, bucket)
        await container.async_uploads.complete(upload_id, CompleteUploadRequest())
        assert (await _until_settled(container, session_id))["status"] == "uploaded"

        upload = await container.async_uploads.repository.read(upload_id)
        upload["expiresAt"] = "2000-01-01T00:00:00+00:00"
        await container.async_uploads.repository.write(upload)
        await container.async_uploads.recover_expired_uploads()

        assert keys["video"] in bucket.objects
        assert keys["caseStudy"] in bucket.objects
        await container.shutdown()

    asyncio.run(_run())


async def _init_gcs(container) -> None:
    await _init(container)
