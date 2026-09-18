from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.dependencies import current_actor, get_container
from app.core.exceptions import AppError
from app.services.container import AppContainer

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _viewer_id(request: Request) -> str:
    """The account whose read state a notification request scopes to.

    Every route here runs behind the auth middleware, so an actor always
    exists; the 401 is defensive, not a path any real request should hit.
    """
    actor = current_actor(request)
    if actor is None:
        raise AppError("Authentication required.", status_code=401)
    return actor.user_id


@router.get("")
async def list_notifications(
    request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, object]:
    """The feed and unread badge, served from cache while nothing has changed.

    One cached payload rather than two queries: the browser fetches this on
    every stream reconnect and change announcement, so under several open tabs
    the uncached version multiplied database reads for data that had not moved.
    Notifications are shared team-wide; unread state is this viewer's own.
    """
    return await container.notifications.feed(viewer_id=_viewer_id(request))


@router.post("/read-all")
async def mark_all_notifications_read(
    request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, object]:
    """Dismiss every notification this viewer has not read, in one request.

    Declared before ``/{notification_id}/read`` only for readability — the two
    cannot collide, since that route needs a second path segment. Scoped to
    the calling viewer: it must never clear another marker's badge. The count
    in the response is re-read rather than assumed to be zero: a notification
    raised while this request ran is still unread, and the badge must say so.
    """
    viewer_id = _viewer_id(request)
    marked = await container.notifications.mark_all_read(viewer_id=viewer_id)
    return {"unreadCount": await container.notifications.unread_count(viewer_id=viewer_id), "markedRead": marked}


@router.post("/{notification_id}/read")
async def mark_notification_read(
    notification_id: str,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    viewer_id = _viewer_id(request)
    if not await container.notifications.mark_read(notification_id, viewer_id=viewer_id):
        raise AppError("Notification not found.", status_code=404)
    return {"unreadCount": await container.notifications.unread_count(viewer_id=viewer_id)}
