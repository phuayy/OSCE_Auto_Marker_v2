"""Selects the object-storage backend for this deployment.

``STORAGE_BACKEND`` is the single switch: ``local`` keeps uploaded bytes on this
machine and serves them from ``/media``; ``gcs`` hands the browser a resumable
session URI so bytes never traverse the API process, and materialises objects
onto worker disk on demand. Everything above this function is written against
``ObjectStorage`` and does not know which one it holds.
"""

from __future__ import annotations

from app.core.config import Settings
from app.core.exceptions import AppError
from app.storage.base import ObjectStorage
from app.storage.gcs import GcsObjectStorageService
from app.storage.local import LocalObjectStorageService


SUPPORTED_BACKENDS = ("local", "gcs")


def create_storage_service(settings: Settings) -> ObjectStorage:
    backend = (settings.storage_backend or "local").strip().lower()
    if backend == "local":
        return LocalObjectStorageService(settings)
    if backend == "gcs":
        return GcsObjectStorageService(settings)
    raise AppError(
        f"Unsupported STORAGE_BACKEND: {settings.storage_backend}. "
        f"Supported backends: {', '.join(SUPPORTED_BACKENDS)}.",
        status_code=500,
    )


__all__ = ["SUPPORTED_BACKENDS", "create_storage_service"]
