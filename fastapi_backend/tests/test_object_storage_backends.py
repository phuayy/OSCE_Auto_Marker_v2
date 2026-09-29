"""Object-storage backend selection and the guarantees each backend owes.

The cloud backend exists so a large upload survives an API restart: the browser
PUTs at a resumable session URI instead of relaying every chunk through this
process. The safety property that makes that acceptable is that the *server*
mints the object key and re-reads the object by that key at completion — a
caller never hands the backend a location to go and fetch. These tests pin both
the selection wiring and that key discipline.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import Settings
from app.core.exceptions import AppError
from app.storage import (
    GcsObjectStorageService,
    LocalObjectStorageService,
    create_storage_service,
)


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(root_dir=tmp_path, backend_root=tmp_path, **overrides)


def test_the_backend_is_chosen_by_storage_backend(tmp_path) -> None:
    assert isinstance(create_storage_service(_settings(tmp_path)), LocalObjectStorageService)
    gcs = create_storage_service(
        _settings(tmp_path, storage_backend="gcs", gcs_bucket="osce-uploads")
    )
    assert isinstance(gcs, GcsObjectStorageService)
    assert (gcs.provider, gcs.strategy) == ("gcs", "gcs_resumable")


def test_an_unknown_backend_is_refused_at_construction(tmp_path) -> None:
    with pytest.raises(AppError) as error:
        create_storage_service(_settings(tmp_path, storage_backend="s3"))
    assert "Unsupported STORAGE_BACKEND" in error.value.message


def test_the_gcs_backend_refuses_to_start_without_a_bucket(tmp_path) -> None:
    """Fail at boot, not on the first upload an hour into a deployment."""
    with pytest.raises(AppError) as error:
        create_storage_service(_settings(tmp_path, storage_backend="gcs"))
    assert "GCS_BUCKET" in error.value.message


@pytest.mark.parametrize(
    "key",
    ["../../etc/passwd", "sessions/../../escape", "", "/", "..\\\\windows\\\\system32"],
)
def test_a_key_that_could_escape_the_cache_is_rejected(tmp_path, key: str) -> None:
    """Keys are server-minted, so a bad one means a tampered record.

    Rejecting it here is what stops a doctored ``storageRef`` from writing its
    download outside the object cache.
    """
    storage = create_storage_service(
        _settings(tmp_path, storage_backend="gcs", gcs_bucket="osce-uploads")
    )
    with pytest.raises(AppError):
        storage._cache_path(key)


def test_a_legitimate_key_stays_inside_the_object_cache(tmp_path) -> None:
    settings = _settings(tmp_path, storage_backend="gcs", gcs_bucket="osce-uploads")
    storage = create_storage_service(settings)
    cached = storage._cache_path("sessions/abc/source/video/station.mp4")
    assert cached.is_relative_to(settings.gcs_cache_root)


def test_relaying_parts_through_the_api_is_refused_on_the_cloud_backend(tmp_path) -> None:
    """A client built for the local backend must fail loudly, not silently.

    Without this the bytes would go nowhere and `complete` would report a file
    that was never received, with nothing pointing at the real cause.
    """
    storage = create_storage_service(
        _settings(tmp_path, storage_backend="gcs", gcs_bucket="osce-uploads")
    )
    upload = {"id": "u1", "files": [{"fileId": "f1", "kind": "video", "key": "k"}]}
    with pytest.raises(AppError) as error:
        asyncio.run(storage.put_part(upload, "f1", 1, b"bytes"))
    assert error.value.status_code == 409
    assert "uploadUrl" in error.value.message


def test_local_materialize_returns_the_stored_path(tmp_path) -> None:
    storage = create_storage_service(_settings(tmp_path))
    stored = tmp_path / "objects" / "video.mp4"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(b"payload")

    resolved = asyncio.run(storage.materialize({"localPath": str(stored)}))
    assert resolved == stored


def test_local_materialize_refuses_a_path_that_is_gone(tmp_path) -> None:
    """Better a clear storage error than ffmpeg failing on a phantom filename."""
    storage = create_storage_service(_settings(tmp_path))
    with pytest.raises(AppError) as error:
        asyncio.run(storage.materialize({"localPath": str(tmp_path / "missing.mp4")}))
    assert error.value.status_code == 500


def test_local_prepare_session_sources_resolves_both_source_files(tmp_path) -> None:
    storage = create_storage_service(_settings(tmp_path))
    session = {
        "files": {
            "video": {"storageRef": {"localPath": str(tmp_path / "v.mp4")}},
            "caseStudy": {"storageRef": {"localPath": str(tmp_path / "c.pdf")}},
        }
    }
    resolved = asyncio.run(storage.prepare_session_sources(session))
    assert resolved["files"]["video"]["absolutePath"] == str(tmp_path / "v.mp4")
    assert resolved["files"]["caseStudy"]["absolutePath"] == str(tmp_path / "c.pdf")


# ----------------------------------------------------------------------
# delete_committed_object — audit finding 4 (session retention/deletion had
# no storage operation to delete a committed object, only the local path).
# ----------------------------------------------------------------------


def test_local_delete_committed_object_removes_the_file(tmp_path) -> None:
    storage = create_storage_service(_settings(tmp_path))
    stored = storage.settings.object_storage_root / "sessions" / "s1" / "source" / "video" / "station.mp4"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(b"payload")

    ref = {"provider": "local", "key": "sessions/s1/source/video/station.mp4"}
    assert asyncio.run(storage.delete_committed_object(ref)) is True
    assert not stored.exists()


def test_local_delete_committed_object_refuses_a_key_escaping_the_root(tmp_path) -> None:
    storage = create_storage_service(_settings(tmp_path))
    ref = {"provider": "local", "key": "../../etc/passwd"}
    with pytest.raises(AppError):
        asyncio.run(storage.delete_committed_object(ref))


def test_local_delete_committed_object_missing_file_is_still_success(tmp_path) -> None:
    """Already gone counts as gone — the caller must not treat this as a failure."""
    storage = create_storage_service(_settings(tmp_path))
    ref = {"provider": "local", "key": "sessions/s1/source/video/never-existed.mp4"}
    assert asyncio.run(storage.delete_committed_object(ref)) is True


def test_local_delete_committed_object_declines_a_foreign_ref(tmp_path) -> None:
    """A ref this backend did not mint is not this backend's to delete."""
    storage = create_storage_service(_settings(tmp_path))
    assert asyncio.run(storage.delete_committed_object({"provider": "gcs", "key": "x"})) is False
    assert asyncio.run(storage.delete_committed_object({"key": "x"})) is False


class _FakeGcsBlob:
    def __init__(self, name: str, data: bytes = b"payload") -> None:
        self.name = name
        self.data = data

    @property
    def size(self) -> int:
        return len(self.data)


class _FakeGcsBucket:
    """Minimal double for the calls delete_committed_object makes."""

    def __init__(self) -> None:
        self.objects: dict[str, _FakeGcsBlob] = {}
        self.deleted: list[tuple[str, object]] = []
        self.fail_with: Exception | None = None

    def delete_blob(self, name: str, if_generation_match=None, **_kwargs) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        if name not in self.objects:
            from google.api_core.exceptions import NotFound

            raise NotFound(f"no such object: {name}")
        self.deleted.append((name, if_generation_match))
        del self.objects[name]


def _gcs_storage(tmp_path):
    storage = create_storage_service(
        _settings(tmp_path, storage_backend="gcs", gcs_bucket="osce-uploads")
    )
    bucket = _FakeGcsBucket()
    storage._bucket = bucket
    return storage, bucket


def test_gcs_delete_committed_object_passes_the_generation_precondition(tmp_path) -> None:
    storage, bucket = _gcs_storage(tmp_path)
    key = "sessions/s1/source/video/station.mp4"
    bucket.objects[key] = _FakeGcsBlob(key)

    assert asyncio.run(storage.delete_committed_object({"provider": "gcs", "key": key, "generation": "42"})) is True
    assert bucket.deleted == [(key, 42)]
    assert not storage._cache_path(key).exists()


def test_gcs_delete_committed_object_missing_object_is_success(tmp_path) -> None:
    storage, bucket = _gcs_storage(tmp_path)
    key = "sessions/s1/source/video/gone.mp4"

    assert asyncio.run(storage.delete_committed_object({"provider": "gcs", "key": key})) is True
    assert bucket.deleted == []


def test_gcs_delete_committed_object_generation_mismatch_leaves_it_in_place(tmp_path) -> None:
    """A PreconditionFailed means the object at this key is a different
    generation than the one this ref committed — not ours to delete."""
    from google.api_core.exceptions import PreconditionFailed

    storage, bucket = _gcs_storage(tmp_path)
    key = "sessions/s1/source/video/station.mp4"
    bucket.objects[key] = _FakeGcsBlob(key)
    bucket.fail_with = PreconditionFailed("generation mismatch")

    assert asyncio.run(storage.delete_committed_object({"provider": "gcs", "key": key, "generation": "7"})) is True
    assert key in bucket.objects  # the object itself was not touched
    assert bucket.deleted == []


def test_gcs_delete_committed_object_other_failures_propagate(tmp_path) -> None:
    storage, bucket = _gcs_storage(tmp_path)
    key = "sessions/s1/source/video/station.mp4"
    bucket.objects[key] = _FakeGcsBlob(key)
    bucket.fail_with = RuntimeError("simulated bucket outage")

    with pytest.raises(RuntimeError):
        asyncio.run(storage.delete_committed_object({"provider": "gcs", "key": key}))


def test_gcs_delete_committed_object_removes_the_cache_copy(tmp_path) -> None:
    storage, bucket = _gcs_storage(tmp_path)
    key = "sessions/s1/source/video/station.mp4"
    bucket.objects[key] = _FakeGcsBlob(key)
    cache_path = storage._cache_path(key)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(b"cached")

    asyncio.run(storage.delete_committed_object({"provider": "gcs", "key": key}))
    assert not cache_path.exists()


def test_gcs_delete_committed_object_declines_a_foreign_ref(tmp_path) -> None:
    storage, _bucket = _gcs_storage(tmp_path)
    assert asyncio.run(storage.delete_committed_object({"provider": "local", "key": "x"})) is False
    assert asyncio.run(storage.delete_committed_object({"key": "x"})) is False
