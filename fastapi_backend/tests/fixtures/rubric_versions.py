"""An in-memory stand-in for ``CommunicationRubricVersionRepository``.

For the ``RubricService`` unit tests that fake the asset registry (their asset
ids are not rows a foreign key could point at). It builds real
``CommunicationRubricVersionRecord`` objects and answers through the
repository's own projections, so what it returns has the shape the real one
returns; ``test_communication_rubric_versions.py`` runs the real repository.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.database.models import CommunicationRubricVersionRecord, utc_now
from app.domain.communication_rubric import RubricChangeKind, RubricVersionStatus, rubric_fingerprint, stored_document
from app.repositories.communication_rubric_version_repository import _full, _summary


class InMemoryRubricVersions:
    def __init__(self) -> None:
        self.records: list[CommunicationRubricVersionRecord] = []

    async def active(self) -> dict[str, Any] | None:
        for record in self.records:
            if record.status == RubricVersionStatus.ACTIVE:
                return _full(record)
        return None

    async def get(self, version_id: str) -> dict[str, Any] | None:
        for record in self.records:
            if record.id == version_id:
                return _full(record)
        return None

    async def list_versions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        ordered = sorted(self.records, key=lambda r: r.version_number, reverse=True)
        return [_summary(record) for record in ordered[:limit]]

    async def activate(
        self,
        document: dict[str, Any],
        *,
        change_kind: RubricChangeKind,
        rubric_asset_id: str | None,
        created_by: dict[str, Any] | None = None,
        restored_from_version: int | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        for record in self.records:
            if record.status == RubricVersionStatus.ACTIVE:
                record.status = RubricVersionStatus.RETIRED
                record.retired_at = now
        stored = stored_document(document)
        record = CommunicationRubricVersionRecord(
            id=str(uuid4()),
            version_number=len(self.records) + 1,
            status=RubricVersionStatus.ACTIVE,
            change_kind=RubricChangeKind(change_kind),
            restored_from_version=restored_from_version,
            rubric_asset_id=rubric_asset_id,
            rubric_revision=stored.get("rubric_revision") or rubric_fingerprint(stored),
            document_json=stored,
            created_by_json=created_by,
            created_at=now,
            retired_at=None,
        )
        self.records.append(record)
        return _full(record)
