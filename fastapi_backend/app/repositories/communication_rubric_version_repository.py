"""The communication rubric's version history (``communication_rubric_versions``).

Append-only: a version row is written once and only ever changes from
``active`` to ``retired``. ``activate`` is the one write — it retires the
active row and inserts the next number in a single transaction, so the table
never holds two active versions or none once it holds any.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, update

from app.database.models import CommunicationRubricVersionRecord, utc_now
from app.database.orm import OrmDatabase
from app.domain.communication_rubric import (
    RubricChangeKind,
    RubricVersionStatus,
    rubric_fingerprint,
    stored_document,
)

MAX_LIST_LIMIT = 200


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _summary(record: CommunicationRubricVersionRecord) -> dict[str, Any]:
    """What the version list shows: enough to choose a version without its document."""
    document = record.document_json or {}
    extraction = document.get("extraction") if isinstance(document.get("extraction"), dict) else {}
    return {
        "id": record.id,
        "versionNumber": record.version_number,
        "status": record.status,
        "changeKind": record.change_kind,
        "restoredFromVersion": record.restored_from_version,
        "rubricAssetId": record.rubric_asset_id,
        "rubricRevision": record.rubric_revision,
        "title": document.get("title"),
        "criteriaCount": document.get("criteria_count"),
        "maxScore": document.get("max_score"),
        "passThreshold": document.get("pass_threshold"),
        "extractionMethod": extraction.get("method") or "deterministic",
        "createdBy": record.created_by_json,
        "createdAt": _iso(record.created_at),
        "retiredAt": _iso(record.retired_at),
    }


def _full(record: CommunicationRubricVersionRecord) -> dict[str, Any]:
    return {**_summary(record), "rubric": dict(record.document_json or {})}


class CommunicationRubricVersionRepository:
    def __init__(self, database: OrmDatabase) -> None:
        self.database = database

    async def active(self) -> dict[str, Any] | None:
        """The active version with its document, or None before the first one."""
        async with self.database.session() as db:
            record = await db.scalar(
                select(CommunicationRubricVersionRecord).where(
                    CommunicationRubricVersionRecord.status == RubricVersionStatus.ACTIVE
                )
            )
            return _full(record) if record is not None else None

    async def get(self, version_id: str) -> dict[str, Any] | None:
        async with self.database.session() as db:
            record = await db.get(CommunicationRubricVersionRecord, version_id)
            return _full(record) if record is not None else None

    async def list_versions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Newest first, without documents."""
        bounded = max(1, min(int(limit), MAX_LIST_LIMIT))
        async with self.database.session() as db:
            rows = await db.scalars(
                select(CommunicationRubricVersionRecord)
                .order_by(CommunicationRubricVersionRecord.version_number.desc())
                .limit(bounded)
            )
            return [_summary(record) for record in rows]

    async def activate(
        self,
        document: dict[str, Any],
        *,
        change_kind: RubricChangeKind,
        rubric_asset_id: str | None,
        created_by: dict[str, Any] | None = None,
        restored_from_version: int | None = None,
    ) -> dict[str, Any]:
        """Retire the active version and make ``document`` the next one.

        One transaction, with the write lock taken before the read of the
        current maximum (``unit_of_work``), so two activations cannot claim
        the same number or leave two rows active; the unique constraints are
        the backstop.
        """
        stored = stored_document(document)
        now = utc_now()
        async with self.database.unit_of_work() as db:
            await db.execute(
                update(CommunicationRubricVersionRecord)
                .where(CommunicationRubricVersionRecord.status == RubricVersionStatus.ACTIVE)
                .values(status=RubricVersionStatus.RETIRED, retired_at=now)
            )
            # The retire must reach the database before the insert: the partial
            # unique index allows only one active row at a time.
            await db.flush()
            highest = await db.scalar(select(func.max(CommunicationRubricVersionRecord.version_number)))
            record = CommunicationRubricVersionRecord(
                id=str(uuid4()),
                version_number=int(highest or 0) + 1,
                status=RubricVersionStatus.ACTIVE,
                change_kind=RubricChangeKind(change_kind),
                restored_from_version=restored_from_version,
                rubric_asset_id=rubric_asset_id,
                rubric_revision=stored.get("rubric_revision") or rubric_fingerprint(stored),
                document_json=stored,
                created_by_json=created_by,
                created_at=now,
            )
            db.add(record)
            await db.flush()
            return _full(record)
