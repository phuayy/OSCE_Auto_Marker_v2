from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, delete, exists, func, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.database.models import NotificationReadRecord, NotificationRecord, utc_now
from app.database.orm import OrmDatabase


logger = logging.getLogger(__name__)


def _record_to_dict(record: NotificationRecord, *, read: bool) -> dict[str, Any]:
    return {
        "id": record.id,
        "sessionId": record.session_id,
        "eventType": record.event_type,
        "title": record.title,
        "body": record.body,
        "createdAt": record.created_at.isoformat() if record.created_at else None,
        "read": read,
    }


def _unread_predicate(viewer_id: str):
    """``NOT EXISTS`` a read marker for this viewer — the one predicate every
    read-path query below filters or counts on."""
    already_read = (
        select(NotificationReadRecord.notification_id)
        .where(
            NotificationReadRecord.notification_id == NotificationRecord.id,
            NotificationReadRecord.user_id == viewer_id,
        )
        .correlate(NotificationRecord)
    )
    return ~exists(already_read)


class NotificationRepository:
    """Notifications are shared (every marker sees every session, so every
    marker sees every notification — see ``app/domain/access.py``); read
    state is not. Every read/write below is scoped to one ``viewer_id``
    against ``notification_reads``, never to the legacy ``notifications.read_at``
    column, which a single viewer's dismissal used to clear for everyone.
    """

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
        of the browser having to re-fetch to learn what just happened. Freshly
        created, so it is unread for every viewer by construction.

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
        return _record_to_dict(record, read=False)

    async def notify(self, title: str, body: str, *, session_id: str | None = None) -> None:
        """Record a notification, discarding the stored row.

        Retained as the original call shape used before typed events existed;
        :meth:`create` is the one to use in new code.
        """
        await self.create(title, body, session_id=session_id)

    async def list_rows(self, limit: int = 200, *, viewer_id: str) -> list[dict[str, Any]]:
        async with self.database.session() as db:
            rows = (
                await db.execute(
                    select(NotificationRecord, NotificationReadRecord.notification_id)
                    .outerjoin(
                        NotificationReadRecord,
                        and_(
                            NotificationReadRecord.notification_id == NotificationRecord.id,
                            NotificationReadRecord.user_id == viewer_id,
                        ),
                    )
                    .order_by(NotificationRecord.created_at.desc())
                    .limit(limit)
                )
            ).all()
        return [_record_to_dict(record, read=read_marker is not None) for record, read_marker in rows]

    async def unread_count(self, *, viewer_id: str) -> int:
        async with self.database.session() as db:
            return int(
                await db.scalar(
                    select(func.count(NotificationRecord.id)).where(_unread_predicate(viewer_id))
                )
                or 0
            )

    async def delete_for_session(self, session_id: str) -> int:
        async with self.database.transaction() as db:
            result = await db.execute(
                delete(NotificationRecord).where(NotificationRecord.session_id == session_id)
            )
        return int(result.rowcount or 0)

    async def mark_read(self, notification_id: str, *, viewer_id: str) -> bool:
        async with self.database.transaction() as db:
            notification = await db.get(NotificationRecord, notification_id)
            if notification is None:
                return False
            await db.execute(self._upsert_read(notification_id, viewer_id))
        return True

    async def mark_all_read(self, *, viewer_id: str) -> int:
        """Mark every notification this viewer has not yet read. Returns how
        many rows changed.

        One INSERT...SELECT rather than a loop over :meth:`mark_read`: on
        PostgreSQL the statement-level trigger then bumps the change counter
        once and announces one change, so every listening process evicts this
        viewer's cached feed once and their browser refetches once, instead of
        once per notification. Idempotent by construction — a notification
        already read by this viewer is excluded by the same predicate
        :meth:`unread_count` uses, so a retry after a lost response changes
        nothing and reports 0.
        """
        select_unread = select(
            NotificationRecord.id,
            literal(viewer_id),
            literal(utc_now()),
        ).where(_unread_predicate(viewer_id))
        async with self.database.transaction() as db:
            result = await db.execute(
                NotificationReadRecord.__table__.insert().from_select(
                    ["notification_id", "user_id", "read_at"], select_unread
                )
            )
        return int(result.rowcount or 0)

    def _upsert_read(self, notification_id: str, viewer_id: str):
        """Insert-or-ignore a (notification, viewer) read marker — a repeat
        mark-read of the same notification by the same viewer must not raise
        on the primary key."""
        values = {"notification_id": notification_id, "user_id": viewer_id, "read_at": utc_now()}
        insert = pg_insert if self.database.engine.dialect.name == "postgresql" else sqlite_insert
        stmt = insert(NotificationReadRecord.__table__).values(**values)
        return stmt.on_conflict_do_nothing(index_elements=["notification_id", "user_id"])
