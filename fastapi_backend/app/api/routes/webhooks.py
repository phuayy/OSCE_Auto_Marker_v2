from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import get_container, require_admin
from app.core.exceptions import AppError
from app.core.webhook_url import WebhookUrlError, validate_webhook_url
from app.domain.notifications import SUBSCRIBABLE_EVENT_TYPES, NotificationType
from app.schemas.webhooks import WebhookPayload
from app.services.container import AppContainer


# Webhook subscriptions carry signing secrets and arbitrary destination URLs —
# deployment-wide config (see CLAUDE.md "Two-tier settings"), not something a
# marker registers. The whole router is admin-only, reads included: unlike
# corpora or the rubric, nothing here is a marker's input to a run.
router = APIRouter(prefix="/admin/webhooks", tags=["webhooks"], dependencies=[Depends(require_admin)])


def _validated_url(payload: WebhookPayload, container: AppContainer) -> str:
    """Shape-checked URL plus the runtime safety check, as one 400 on failure."""
    try:
        return validate_webhook_url(
            payload.url,
            allow_private=container.settings.webhook_allow_private_urls,
        )
    except WebhookUrlError as error:
        raise AppError(str(error), status_code=400) from error


@router.get("")
async def list_webhooks(container: AppContainer = Depends(get_container)) -> dict[str, object]:
    """Registered subscriptions plus the event types available to subscribe to.

    Secrets are returned masked; the UI renders the catalogue from
    ``eventTypes`` so a newly added event type needs no frontend change.
    """
    return {
        "webhooks": await container.webhooks.list_rows(),
        "eventTypes": list(SUBSCRIBABLE_EVENT_TYPES),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_webhook(
    payload: WebhookPayload,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    """Register an endpoint. The response carries the signing secret **once** —
    it is masked on every subsequent read and cannot be recovered."""
    url = _validated_url(payload, container)
    webhook = await container.webhooks.create(
        url=url,
        description=payload.description,
        event_types=payload.eventTypes,
        active=payload.active,
    )
    return {"webhook": webhook}


@router.put("/{webhook_id}")
async def update_webhook(
    webhook_id: str,
    payload: WebhookPayload,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    url = _validated_url(payload, container)
    webhook = await container.webhooks.update(
        webhook_id,
        url=url,
        description=payload.description,
        event_types=payload.eventTypes,
        active=payload.active,
    )
    if webhook is None:
        raise AppError("Webhook not found.", status_code=404)
    return {"webhook": webhook}


@router.post("/{webhook_id}/rotate-secret")
async def rotate_webhook_secret(
    webhook_id: str,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    """Issue a new signing secret, returned once. The old one stops working
    immediately, so the subscriber must be updated before the next event."""
    webhook = await container.webhooks.rotate_secret(webhook_id)
    if webhook is None:
        raise AppError("Webhook not found.", status_code=404)
    return {"webhook": webhook}


@router.delete("/{webhook_id}")
async def delete_webhook(
    webhook_id: str,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    if not await container.webhooks.delete(webhook_id):
        raise AppError("Webhook not found.", status_code=404)
    return {"deleted": True}


@router.post("/{webhook_id}/test")
async def test_webhook(
    webhook_id: str,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    """Send a signed test event to this endpoint only, and await the result.

    Deliberately synchronous, unlike a real event: the operator clicked a button
    and needs the answer, and the delivery log row records what happened either
    way.
    """
    subscription = await container.webhooks.get_with_secret(webhook_id)
    if subscription is None:
        raise AppError("Webhook not found.", status_code=404)

    delivered = await container.webhook_dispatcher.deliver(
        subscription,
        NotificationType.WEBHOOK_TEST.value,
        {
            "id": f"test-{webhook_id}",
            "sessionId": None,
            "title": "Test event",
            "body": "This is a test event from the OSCE AI Marker.",
            "createdAt": None,
        },
    )
    return {
        "delivered": delivered,
        "webhook": await container.webhooks.get(webhook_id),
    }


@router.get("/{webhook_id}/deliveries")
async def list_webhook_deliveries(
    webhook_id: str,
    limit: int = Query(50, ge=1, le=200),
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    if await container.webhooks.get(webhook_id) is None:
        raise AppError("Webhook not found.", status_code=404)
    return {"deliveries": await container.webhooks.list_deliveries(webhook_id, limit)}
