from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_container
from app.core.exceptions import AppError
from app.services.container import AppContainer

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("")
async def list_notifications(container: AppContainer = Depends(get_container)) -> dict[str, object]:
    return {
        "notifications": await container.notifications.list_rows(),
        "unreadCount": await container.notifications.unread_count(),
    }


@router.post("/{notification_id}/read")
async def mark_notification_read(
    notification_id: str,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    if not await container.notifications.mark_read(notification_id):
        raise AppError("Notification not found.", status_code=404)
    return {"unreadCount": await container.notifications.unread_count()}
