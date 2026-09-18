from __future__ import annotations

from ipaddress import ip_address, ip_network
from typing import Any, Callable

from fastapi import Depends, HTTPException, Request, status

from app.domain.access import ensure_may_mutate
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
    if payload is None and request.method in {"GET", "HEAD"} and (is_media or is_stream_path(path)):
        payload = await container.auth.verify_stream_ticket(extract_stream_ticket(request))
    if not payload:
        return False, None
    return True, payload


def extract_bearer_token(request: Request) -> str:
    header_value = str(request.headers.get("authorization") or "").strip()
    if header_value.lower().startswith("bearer "):
        return header_value[7:].strip()
    return ""


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


def require_expensive_operation(
    request: Request, container: AppContainer = Depends(get_container)
) -> None:
    actor = current_actor(request)
    if actor is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    container.expensive_operation_rate_limiter.check(actor.user_id)


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


# --- ownership gates ---------------------------------------------------------
#
# A sibling of require_admin for the other half of authorization: not "which
# role", but "did YOU create this". Each reads the path parameter FastAPI has
# already parsed onto ``request.path_params`` (routing runs before
# dependencies), loads the record, and raises through ``ensure_may_mutate``
# (app.domain.access) — 403, or 404 via the same not-found translation the
# routes already use for a missing id.
# tests/test_session_routes_are_guarded.py recognises these three by direct
# identity against a tuple, the same way test_admin_routes_are_guarded.py
# recognises require_admin — no marker attribute needed for that.


async def require_session_owner(
    request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    session_id = str(request.path_params.get("session_id") or "")
    try:
        session = await container.sessions.read(session_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found.") from error
    ensure_may_mutate(session, current_actor(request), subject="session")
    return session


async def require_job_owner(request: Request, container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    job_id = str(request.path_params.get("job_id") or "")
    try:
        job = await container.jobs.repository.read(job_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="Job not found.") from error
    session_id = str(job.get("sessionId") or "")
    session: dict[str, Any] | None = None
    if session_id:
        try:
            session = await container.sessions.read(session_id)
        except FileNotFoundError:
            session = None
    if session is not None:
        ensure_may_mutate(session, current_actor(request), subject="session")
    return job


async def require_upload_owner(
    request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    upload_id = str(request.path_params.get("upload_id") or "")
    try:
        upload = await container.async_uploads.repository.read(upload_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="Upload not found.") from error
    ensure_may_mutate(upload, current_actor(request), subject="upload")
    return upload


def client_ip(request: Request, trusted_proxy_count: int = 0, trusted_proxy_ips: tuple[str, ...] = ()) -> str:
    """The address the rate limiters key on.

    With no proxy the socket address is the client. Behind ``n`` trusted
    proxies, the originating address is the ``n``-th entry from the *right* of
    X-Forwarded-For: each proxy appends the peer it saw, so the rightmost
    entries are the ones our own infrastructure wrote and the leftmost are
    whatever the client chose to send. Counting from the right is what stops a
    client from forging its way into a fresh rate-limit bucket per request —
    *if* the request actually traversed that many real proxies.

    Hop-counting alone cannot verify that: a client that reaches the API port
    directly with one forged X-Forwarded-For entry produces a header
    indistinguishable in shape from the legitimate single-proxy case (both are
    exactly ``trusted_proxy_count`` hops long). ``trusted_proxy_ips``, when
    given, closes that gap the way nginx's own ``set_real_ip_from`` or
    Django's proxy trust list do: the header is honoured only when the
    *immediate* TCP peer — ``request.client.host``, which a client cannot
    forge, unlike anything in a header it sends — is itself one of the
    configured proxy addresses. Left empty (the default), the header is
    trusted by count alone, unchanged from before this parameter existed;
    ``Settings.collect_runtime_warnings`` flags that combination.
    """
    socket_host = request.client.host if request.client else "unknown"
    if trusted_proxy_count <= 0:
        return socket_host
    if trusted_proxy_ips and not _peer_is_trusted(socket_host, trusted_proxy_ips):
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


def _peer_is_trusted(socket_host: str, trusted_proxy_ips: tuple[str, ...]) -> bool:
    """Whether the direct TCP peer is a configured reverse-proxy address.

    Malformed entries on either side (an unparseable peer, an unparseable
    configured network) are simply not a match rather than a startup error —
    ``client_ip`` runs on every request, and a typo here must fail closed
    (untrusted) rather than crash request handling.
    """
    try:
        peer = ip_address(socket_host)
    except ValueError:
        return False
    for entry in trusted_proxy_ips:
        try:
            if peer in ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False
