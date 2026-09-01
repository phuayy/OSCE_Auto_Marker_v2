from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select

from app.database.models import NotificationRecord, utc_now
from app.database.orm import OrmDatabase


logger = logging.getLogger(__name__)


def _record_to_dict(record: NotificationRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "sessionId": record.session_id,
        "eventType": record.event_type,
        "title": record.title,
        "body": record.body,
        "createdAt": record.created_at.isoformat() if record.created_at else None,
        "read": record.read_at is not None,
    }


class NotificationRepository:
    def __init__(self, database: OrmDatabase) -> None:
        self.database = database

    async def create(
        self,
        title: str,
        body: str,
        *,
        session_id: str | None = None,
        event_type: str | None = None,
    ) -> dict[str, Any] | None:
        """Persist a notification and return the stored row, or None on failure.

        Returning the row is what lets the caller push the *same* object to SSE
        subscribers and webhook endpoints — id and timestamp included — instead
        of the browser having to re-fetch to learn what just happened.

        Fire-and-forget from pipeline code: a notification failure must never
        fail the task it announces, so persistence errors are logged and
        swallowed, and the caller sees None.
        """
        record = NotificationRecord(
            id=str(uuid4()),
            session_id=session_id,
            event_type=event_type,
            title=title,
            body=body,
        )
        try:
            async with self.database.transaction() as db:
                db.add(record)
        except Exception:
            logger.exception("Failed to record notification %r", title)
            return None
        return _record_to_dict(record)

    async def notify(self, title: str, body: str, *, session_id: str | None = None) -> None:
        """Record a notification, discarding the stored row.

        Retained as the original call shape used before typed events existed;
        :meth:`create` is the one to use in new code.
        """
        await self.create(title, body, session_id=session_id)

    async def list_rows(self, limit: int = 200) -> list[dict[str, Any]]:
        async with self.database.session() as db:
            records = (
                await db.scalars(
                    select(NotificationRecord).order_by(NotificationRecord.created_at.desc()).limit(limit)
                )
            ).all()
        return [_record_to_dict(record) for record in records]

    async def unread_count(self) -> int:
        async with self.database.session() as db:
            return int(
                await db.scalar(select(func.count()).where(NotificationRecord.read_at.is_(None))) or 0
            )

    async def delete_for_session(self, session_id: str) -> int:
        async with self.database.transaction() as db:
            result = await db.execute(
                delete(NotificationRecord).where(NotificationRecord.session_id == session_id)
            )
        return int(result.rowcount or 0)

    async def mark_read(self, notification_id: str) -> bool:
        async with self.database.transaction() as db:
            record = await db.get(NotificationRecord, notification_id)
            if record is None:
                return False
            if record.read_at is None:
                record.read_at = utc_now()
            return True
