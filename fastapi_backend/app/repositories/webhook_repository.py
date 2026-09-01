from __future__ import annotations

import logging
import secrets
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select

from app.database.models import WebhookDeliveryRecord, WebhookSubscriptionRecord, utc_now
from app.database.orm import OrmDatabase


logger = logging.getLogger(__name__)

# Delivery rows retained per subscription. The log answers "did my endpoint get
# the last few events?"; older attempts have no operational value and would grow
# unbounded on a busy install.
DELIVERY_RETENTION_PER_SUBSCRIPTION = 50

# Number of trailing secret characters shown in the masked preview. Enough to
# tell two secrets apart in the UI, far too few to reconstruct one.
_SECRET_PREVIEW_CHARS = 4


def generate_webhook_secret() -> str:
    """A signing secret for a new subscription (32 bytes, URL-safe)."""
    return secrets.token_urlsafe(32)


def mask_secret(secret: str) -> str:
    """``"...xY7q"`` — safe to return from the API on every read.

    The full secret is shown exactly once, in the create response. After that
    only this preview is available, so a leaked API response cannot be used to
    forge signatures.
    """
    value = str(secret or "")
    if len(value) <= _SECRET_PREVIEW_CHARS:
        return "..."
    return f"...{value[-_SECRET_PREVIEW_CHARS:]}"


def subscription_to_dict(record: WebhookSubscriptionRecord, *, include_secret: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": record.id,
        "description": record.description or "",
        "url": record.url,
        "eventTypes": list(record.event_types or []),
        "active": bool(record.active),
        "secretPreview": mask_secret(record.secret),
        "createdAt": record.created_at.isoformat() if record.created_at else None,
        "updatedAt": record.updated_at.isoformat() if record.updated_at else None,
        "lastDeliveryAt": record.last_delivery_at.isoformat() if record.last_delivery_at else None,
        "lastStatusCode": record.last_status_code,
        "lastError": record.last_error,
        "consecutiveFailures": int(record.consecutive_failures or 0),
    }
    if include_secret:
        # Only ever set on the create response.
        payload["secret"] = record.secret
    return payload


def delivery_to_dict(record: WebhookDeliveryRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "subscriptionId": record.subscription_id,
        "notificationId": record.notification_id,
        "eventType": record.event_type,
        "attempt": int(record.attempt or 1),
        "status": record.status,
        "statusCode": record.status_code,
        "error": record.error,
        "durationMs": record.duration_ms,
        "createdAt": record.created_at.isoformat() if record.created_at else None,
    }


class WebhookRepository:
    """Persistence for webhook subscriptions and their delivery log."""

    def __init__(self, database: OrmDatabase) -> None:
        self.database = database

    # -- subscriptions -----------------------------------------------------

    async def list_rows(self) -> list[dict[str, Any]]:
        async with self.database.session() as db:
            records = (
                await db.scalars(
                    select(WebhookSubscriptionRecord).order_by(WebhookSubscriptionRecord.created_at.desc())
                )
            ).all()
        return [subscription_to_dict(record) for record in records]

    async def get(self, subscription_id: str) -> dict[str, Any] | None:
        async with self.database.session() as db:
            record = await db.get(WebhookSubscriptionRecord, subscription_id)
        return subscription_to_dict(record) if record else None

    async def list_active_for_event(self, event_type: str) -> list[dict[str, Any]]:
        """Active subscriptions wanting ``event_type``, secrets included.

        Only the dispatcher calls this — the secret is needed to sign. Filtering
        happens in Python rather than SQL because the JSON-array containment
        operators differ between SQLite and PostgreSQL, and the subscription
        count here is small by nature.
        """
        from app.domain.notifications import event_types_match

        async with self.database.session() as db:
            records = (
                await db.scalars(
                    select(WebhookSubscriptionRecord).where(WebhookSubscriptionRecord.active.is_(True))
                )
            ).all()
        return [
            subscription_to_dict(record, include_secret=True)
            for record in records
            if event_types_match(list(record.event_types or []), event_type)
        ]

    async def get_with_secret(self, subscription_id: str) -> dict[str, Any] | None:
        async with self.database.session() as db:
            record = await db.get(WebhookSubscriptionRecord, subscription_id)
        return subscription_to_dict(record, include_secret=True) if record else None

    async def create(
        self,
        *,
        url: str,
        description: str = "",
        event_types: list[str] | None = None,
        active: bool = True,
        secret: str | None = None,
    ) -> dict[str, Any]:
        record = WebhookSubscriptionRecord(
            id=str(uuid4()),
            url=url,
            description=description or "",
            event_types=list(event_types or []),
            active=active,
            secret=secret or generate_webhook_secret(),
        )
        async with self.database.transaction() as db:
            db.add(record)
        # The plaintext secret is returned exactly once, here.
        return subscription_to_dict(record, include_secret=True)

    async def update(
        self,
        subscription_id: str,
        *,
        url: str,
        description: str = "",
        event_types: list[str] | None = None,
        active: bool = True,
    ) -> dict[str, Any] | None:
        async with self.database.transaction() as db:
            record = await db.get(WebhookSubscriptionRecord, subscription_id)
            if record is None:
                return None
            record.url = url
            record.description = description or ""
            record.event_types = list(event_types or [])
            record.active = active
            record.updated_at = utc_now()
            # Re-enabling or re-pointing an endpoint is the operator saying "try
            # again"; carrying the old failure streak forward would be wrong.
            if active:
                record.consecutive_failures = 0
            return subscription_to_dict(record)

    async def rotate_secret(self, subscription_id: str) -> dict[str, Any] | None:
        async with self.database.transaction() as db:
            record = await db.get(WebhookSubscriptionRecord, subscription_id)
            if record is None:
                return None
            record.secret = generate_webhook_secret()
            record.updated_at = utc_now()
            return subscription_to_dict(record, include_secret=True)

    async def delete(self, subscription_id: str) -> bool:
        async with self.database.transaction() as db:
            record = await db.get(WebhookSubscriptionRecord, subscription_id)
            if record is None:
                return False
            await db.delete(record)
            await db.execute(
                delete(WebhookDeliveryRecord).where(
                    WebhookDeliveryRecord.subscription_id == subscription_id
                )
            )
        return True

    # -- delivery log ------------------------------------------------------

    async def record_delivery(
        self,
        *,
        subscription_id: str,
        event_type: str,
        attempt: int,
        status: str,
        notification_id: str | None = None,
        status_code: int | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Append one attempt to the log and refresh the subscription summary.

        Best-effort: a logging failure must never turn a delivered webhook into
        a failed one, so everything here is wrapped.
        """
        try:
            async with self.database.transaction() as db:
                db.add(
                    WebhookDeliveryRecord(
                        id=str(uuid4()),
                        subscription_id=subscription_id,
                        notification_id=notification_id,
                        event_type=event_type,
                        attempt=attempt,
                        status=status,
                        status_code=status_code,
                        error=error,
                        duration_ms=duration_ms,
                    )
                )
                record = await db.get(WebhookSubscriptionRecord, subscription_id)
                if record is not None:
                    record.last_delivery_at = utc_now()
                    record.last_status_code = status_code
                    record.last_error = error
                    record.consecutive_failures = (
                        0 if status == "succeeded" else int(record.consecutive_failures or 0) + 1
                    )
            await self._prune_deliveries(subscription_id)
        except Exception:
            logger.exception("Could not record webhook delivery for subscription %s.", subscription_id)

    async def _prune_deliveries(self, subscription_id: str) -> None:
        """Keep only the newest ``DELIVERY_RETENTION_PER_SUBSCRIPTION`` rows.

        Done as "select the ids to keep, delete the rest" because SQLite does not
        support ``DELETE ... ORDER BY ... LIMIT`` without a compile-time option.
        """
        async with self.database.transaction() as db:
            keep_ids = (
                await db.scalars(
                    select(WebhookDeliveryRecord.id)
                    .where(WebhookDeliveryRecord.subscription_id == subscription_id)
                    .order_by(WebhookDeliveryRecord.created_at.desc())
                    .limit(DELIVERY_RETENTION_PER_SUBSCRIPTION)
                )
            ).all()
            if len(keep_ids) < DELIVERY_RETENTION_PER_SUBSCRIPTION:
                return
            await db.execute(
                delete(WebhookDeliveryRecord).where(
                    WebhookDeliveryRecord.subscription_id == subscription_id,
                    WebhookDeliveryRecord.id.notin_(list(keep_ids)),
                )
            )

    async def list_deliveries(self, subscription_id: str, limit: int = 50) -> list[dict[str, Any]]:
        async with self.database.session() as db:
            records = (
                await db.scalars(
                    select(WebhookDeliveryRecord)
                    .where(WebhookDeliveryRecord.subscription_id == subscription_id)
                    .order_by(WebhookDeliveryRecord.created_at.desc())
                    .limit(limit)
                )
            ).all()
        return [delivery_to_dict(record) for record in records]
