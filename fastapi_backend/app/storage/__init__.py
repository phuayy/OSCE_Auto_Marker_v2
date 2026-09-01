"""Object storage: one contract, one backend per deployment.

See :mod:`app.storage.base` for the contract every backend satisfies and
:mod:`app.storage.factory` for how ``STORAGE_BACKEND`` picks one.
"""

from app.storage.base import ObjectStorage, PreparedUploadFile, SourceFileKind
from app.storage.factory import SUPPORTED_BACKENDS, create_storage_service
from app.storage.gcs import GcsObjectStorageService
from app.storage.local import LocalObjectStorageService


__all__ = [
    "SUPPORTED_BACKENDS",
    "GcsObjectStorageService",
    "LocalObjectStorageService",
    "ObjectStorage",
    "PreparedUploadFile",
    "SourceFileKind",
    "create_storage_service",
]
