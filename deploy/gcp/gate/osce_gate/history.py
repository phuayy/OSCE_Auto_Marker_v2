"""How long recent starts took, so the page can say "about 3 minutes left".

A start's length is exact, not observed: the VM's ``lastStartTimestamp`` to
the moment the app published ``ready`` (its guest attribute carries the time).
So it does not matter whether anyone was watching the page.

Kept in one small JSON object in a GCS bucket (``GATE_HISTORY_BUCKET``), so
it survives the gate scaling to zero. Without a bucket the gate keeps the
history in memory only, and a cold gate falls back to
``GATE_EXPECTED_START_SECONDS``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_KEEP = 20


class StartHistory(Protocol):
    def durations(self) -> list[float]: ...
    def record(self, start_key: str, seconds: float) -> None: ...


class MemoryHistory:
    def __init__(self) -> None:
        self._entries: dict[str, float] = {}

    def durations(self) -> list[float]:
        return list(self._entries.values())[-_KEEP:]

    def record(self, start_key: str, seconds: float) -> None:
        if start_key in self._entries:
            return
        self._entries[start_key] = seconds
        while len(self._entries) > _KEEP:
            self._entries.pop(next(iter(self._entries)))


class GcsHistory(MemoryHistory):
    """Memory first, the bucket behind it. Every failure degrades to memory:
    an estimate is a nicety and must never break the start page."""

    _OBJECT = "osce-gate/start-history.json"

    def __init__(self, bucket: str, client: Any | None = None) -> None:
        super().__init__()
        try:
            if client is None:
                from google.cloud import storage

                client = storage.Client()
            self._blob = client.bucket(bucket).blob(self._OBJECT)
            if self._blob.exists():
                loaded = json.loads(self._blob.download_as_text())
                self._entries.update({str(k): float(v) for k, v in dict(loaded).items()})
        except Exception:
            logger.warning("Start history unavailable in gs://%s; estimates use the default.", bucket, exc_info=True)
            self._blob = None

    def record(self, start_key: str, seconds: float) -> None:
        if start_key in self._entries:
            return
        super().record(start_key, seconds)
        if self._blob is None:
            return
        try:
            self._blob.upload_from_string(json.dumps(self._entries), content_type="application/json")
        except Exception:
            logger.warning("Could not save the start history.", exc_info=True)
