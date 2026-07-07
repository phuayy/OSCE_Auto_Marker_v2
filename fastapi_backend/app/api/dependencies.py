from __future__ import annotations

from typing import Any

from fastapi import Request

from app.services.container import AppContainer


# API paths reachable without an auth token (liveness/readiness probes, login,
# and the token-check endpoint which validates its own token).
OPEN_API_PATHS = {"/api/health", "/api/health/ready", "/api/auth/login", "/api/auth/me"}


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


def is_session_events_path(path: str) -> bool:
    return path.startswith("/api/sessions/") and path.endswith("/events")


def authorize_request(
    request: Request,
    container: AppContainer,
    *,
    protect_media: bool,
) -> tuple[bool, dict[str, Any] | None]:
    """Resolve auth for an incoming request.

    Returns ``(allowed, payload)``. ``allowed`` False means respond 401. When
    ``allowed`` is True a non-None ``payload`` should be stored on
    ``request.state.auth_user``; a None payload means the path needs no auth.

    Media (``/media/*``) and the SSE events endpoint additionally accept a
    short-lived stream ticket (``?ticket=``) because ``<video>`` tags and
    EventSource cannot send an Authorization header.
    """
    path = request.url.path
    if path in OPEN_API_PATHS:
        return True, None

    is_media = path.startswith("/media/")
    is_api = path.startswith("/api/")
    if not is_media and not is_api:
        return True, None
    if is_media and not protect_media:
        return True, None

    payload = container.auth.verify_token(extract_bearer_token(request))
    if payload is None and (is_media or is_session_events_path(path)):
        payload = container.auth.verify_stream_ticket(extract_stream_ticket(request))
    if not payload:
        return False, None
    return True, payload


def extract_bearer_token(request: Request) -> str:
    header_value = str(request.headers.get("authorization") or "").strip()
    if header_value.lower().startswith("bearer "):
        return header_value[7:].strip()
    query_token = request.query_params.get("token")
    return str(query_token or "").strip()


def extract_stream_ticket(request: Request) -> str:
    """Read a short-lived media/SSE ticket from the ``ticket`` query parameter.

    EventSource and ``<video>``/``<img>`` tags cannot send an Authorization
    header, so they authenticate via this short-lived ticket instead of placing
    the long-lived bearer token in the URL.
    """
    return str(request.query_params.get("ticket") or "").strip()


def get_auth_payload(request: Request) -> dict[str, Any] | None:
    payload = getattr(request.state, "auth_user", None)
    return payload if isinstance(payload, dict) else None
