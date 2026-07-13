from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select

from app.database.models import NotificationRecord, utc_now
from app.database.orm import OrmDatabase


logger = logging.getLogger(__name__)


class NotificationRepository:
    def __init__(self, database: OrmDatabase) -> None:
        self.database = database

    async def notify(self, title: str, body: str, *, session_id: str | None = None) -> None:
        """Record a notification. Fire-and-forget from pipeline code: a
        notification failure must never fail the task it announces."""
        try:
            async with self.database.transaction() as db:
                db.add(NotificationRecord(id=str(uuid4()), session_id=session_id, title=title, body=body))
        except Exception:
            logger.exception("Failed to record notification %r", title)

    async def list_rows(self, limit: int = 200) -> list[dict[str, Any]]:
        async with self.database.session() as db:
            records = (
                await db.scalars(
                    select(NotificationRecord).order_by(NotificationRecord.created_at.desc()).limit(limit)
                )
            ).all()
        return [
            {
                "id": record.id,
                "sessionId": record.session_id,
                "title": record.title,
                "body": record.body,
                "createdAt": record.created_at.isoformat() if record.created_at else None,
                "read": record.read_at is not None,
            }
            for record in records
        ]

    async def unread_count(self) -> int:
        async with self.database.session() as db:
            return int(
                await db.scalar(select(func.count()).where(NotificationRecord.read_at.is_(None))) or 0
            )

    async def mark_read(self, notification_id: str) -> bool:
        async with self.database.transaction() as db:
            record = await db.get(NotificationRecord, notification_id)
            if record is None:
                return False
            if record.read_at is None:
                record.read_at = utc_now()
            return True
