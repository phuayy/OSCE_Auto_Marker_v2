"""Compatibility re-exports for the object-storage package.

The implementation moved to :mod:`app.storage` when a second backend (GCS)
arrived and the local one stopped being the only shape storage could take. This
module keeps the historical import path working; new code should import from
``app.storage`` directly.
"""

from app.storage import (
    GcsObjectStorageService,
    LocalObjectStorageService,
    ObjectStorage,
    PreparedUploadFile,
    SourceFileKind,
    create_storage_service,
)


__all__ = [
    "GcsObjectStorageService",
    "LocalObjectStorageService",
    "ObjectStorage",
    "PreparedUploadFile",
    "SourceFileKind",
    "create_storage_service",
]
