from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.api.dependencies import get_container
from app.services.container import AppContainer


router = APIRouter(tags=["events"])


@router.get("/events")
async def change_events(container: AppContainer = Depends(get_container)) -> StreamingResponse:
    """Global change stream that replaces the frontend's polling loops.

    Emits one event per committed write to a tracked table. On PostgreSQL these
    originate from database triggers, so a write by the Hatchet worker process
    reaches the browser without the API ever polling for it.

    Authenticated like the per-session stream: ``EventSource`` cannot send an
    Authorization header, so the URL carries a short-lived stream ticket.
    """
    return StreamingResponse(
        container.changes.stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/events/versions")
async def change_versions(container: AppContainer = Depends(get_container)) -> dict[str, object]:
    """Current change counters — a cheap way for a client to check for staleness
    without holding a stream open, and the fallback a browser polls (slowly) if
    ``EventSource`` is unavailable.

    Deliberately minimal, the same reasoning as ``/health/ready``: this is a
    marker-facing, unprivileged endpoint (every signed-in account polls it,
    not just admins), so it carries only what the fallback poller reads. Cache
    hit rates and other internals live behind ``GET /api/admin/health/diagnostics``
    instead, admin-only like the rest of that operator detail.
    """
    return {
        "versions": container.changes.versions(),
        "push": container.changes.push_active,
    }
