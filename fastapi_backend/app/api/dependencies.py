from __future__ import annotations

from typing import Any, Callable

from fastapi import HTTPException, Request, status

from app.domain.actors import Actor
from app.domain.users import UserRole
from app.services.container import AppContainer


# API paths reachable without an auth token (liveness/readiness probes, login,
# and the token-check endpoint which validates its own token).
OPEN_API_PATHS = {"/api/health", "/api/health/ready", "/api/auth/login", "/api/auth/me"}

# Path prefixes reachable without a token: the emailed-link flows. The person
# following an invitation or a password-reset link has, by definition, no
# session yet. Each of these endpoints is gated by the token in its own URL and
# by a per-IP rate limit instead.
OPEN_API_PREFIXES = ("/api/auth/invitations/", "/api/auth/password-reset")


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


def is_open_path(path: str) -> bool:
    return path in OPEN_API_PATHS or path.startswith(OPEN_API_PREFIXES)


def is_session_events_path(path: str) -> bool:
    return path.startswith("/api/sessions/") and path.endswith("/events")


def is_stream_path(path: str) -> bool:
    """Endpoints consumed by ``EventSource``, which cannot send an Authorization
    header and therefore also accept a short-lived stream ticket."""
    return path == "/api/events" or is_session_events_path(path)


async def authorize_request(
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

    Async because verification now consults the account row behind the token
    (through a cache the change feed evicts), which is how a disabled account
    is refused on its next request rather than at its token's expiry.
    """
    path = request.url.path
    if is_open_path(path):
        return True, None

    is_media = path.startswith("/media/")
    is_api = path.startswith("/api/")
    if not is_media and not is_api:
        return True, None
    if is_media and not protect_media:
        return True, None

    payload = await container.auth.verify_token(extract_bearer_token(request))
    if payload is None and (is_media or is_stream_path(path)):
        payload = await container.auth.verify_stream_ticket(extract_stream_ticket(request))
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


def current_actor(request: Request) -> Actor | None:
    """Who this request acts as, for services that record or attribute a
    change. None on the open endpoints (no session). Routes pass it on as an
    ``actor=`` keyword rather than handing a service the request."""
    return Actor.from_auth_payload(get_auth_payload(request))


def require_role(*roles: UserRole) -> Callable[[Request], dict[str, Any]]:
    """A dependency that admits only the given roles.

    The middleware has already authenticated the request, so the payload on
    ``request.state`` is trusted; this only reads its ``role``, which
    ``AuthService`` refreshed from the account row. Applied at the *router*
    (``APIRouter(dependencies=[Depends(require_admin)])``) so a new admin route
    cannot be added without it — ``tests/test_admin_routes_are_guarded.py``
    checks that.
    """
    allowed = {role.value for role in roles}

    def _dependency(request: Request) -> dict[str, Any]:
        payload = get_auth_payload(request)
        if payload is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
        if str(payload.get("role") or "") not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Administrator access is required for this action.",
            )
        return payload

    # Named so the route-guard test can recognise the dependency by identity.
    _dependency.__name__ = f"require_role[{','.join(sorted(allowed))}]"
    _dependency.required_roles = frozenset(allowed)  # type: ignore[attr-defined]
    return _dependency


require_admin = require_role(UserRole.ADMIN)


def client_ip(request: Request, trusted_proxy_count: int = 0) -> str:
    """The address the rate limiters key on.

    With no proxy the socket address is the client. Behind ``n`` trusted
    proxies, the originating address is the ``n``-th entry from the *right* of
    X-Forwarded-For: each proxy appends the peer it saw, so the rightmost
    entries are the ones our own infrastructure wrote and the leftmost are
    whatever the client chose to send. Counting from the right is what stops a
    client from forging its way into a fresh rate-limit bucket per request.
    """
    socket_host = request.client.host if request.client else "unknown"
    if trusted_proxy_count <= 0:
        return socket_host

    forwarded = str(request.headers.get("x-forwarded-for") or "")
    hops = [item.strip() for item in forwarded.split(",") if item.strip()]
    if not hops:
        return socket_host
    # One proxy -> the last entry; two -> the second-to-last, and so on. A
    # shorter chain than configured means the request did not traverse them all,
    # so fall back to the leftmost entry we actually have.
    index = max(0, len(hops) - trusted_proxy_count)
    return hops[index] if index < len(hops) else hops[0]
