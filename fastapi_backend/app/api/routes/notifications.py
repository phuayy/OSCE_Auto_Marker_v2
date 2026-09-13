from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_container
from app.core.exceptions import AppError
from app.services.container import AppContainer

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("")
async def list_notifications(container: AppContainer = Depends(get_container)) -> dict[str, object]:
    """The feed and unread badge, served from cache while nothing has changed.

    One cached payload rather than two queries: the browser fetches this on
    every stream reconnect and change announcement, so under several open tabs
    the uncached version multiplied database reads for data that had not moved.
    """
    return await container.notifications.feed()


@router.post("/read-all")
async def mark_all_notifications_read(container: AppContainer = Depends(get_container)) -> dict[str, object]:
    """Dismiss every unread notification in one request.

    Declared before ``/{notification_id}/read`` only for readability — the two
    cannot collide, since that route needs a second path segment. The count in
    the response is re-read rather than assumed to be zero: a notification
    raised while this request ran is still unread, and the badge must say so.
    """
    marked = await container.notifications.mark_all_read()
    return {"unreadCount": await container.notifications.unread_count(), "markedRead": marked}


@router.post("/{notification_id}/read")
async def mark_notification_read(
    notification_id: str,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    if not await container.notifications.mark_read(notification_id):
        raise AppError("Notification not found.", status_code=404)
    return {"unreadCount": await container.notifications.unread_count()}
