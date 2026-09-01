from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.dependencies import extract_bearer_token, get_container
from app.schemas.auth import (
    AuthMeResponse,
    AuthResponse,
    LoginRequest,
    LogoutResponse,
    StreamTicketResponse,
)
from app.services.container import AppContainer


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=AuthResponse)
async def login(
    request: Request,
    payload: LoginRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    container.login_rate_limiter.check(_client_ip(request, container.settings.trusted_proxy_count))
    result = await container.auth.authenticate(payload.username, payload.password)
    if not result:
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    return result


@router.get("/me", response_model=AuthMeResponse)
async def me(request: Request, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    token = extract_bearer_token(request)
    payload = container.auth.verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return {"username": payload["username"], "expiresAt": payload["expiresAt"]}


@router.get("/stream-ticket", response_model=StreamTicketResponse)
async def stream_ticket(request: Request, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    """Mint a short-lived ticket for SSE/media URLs (requires a valid bearer token)."""
    token = extract_bearer_token(request)
    payload = container.auth.verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return container.auth.issue_stream_ticket(str(payload["username"]))


@router.post("/logout", response_model=LogoutResponse)
async def logout(request: Request, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    """Server-side logout: revoke the presented token so it cannot be reused."""
    revoked = container.auth.revoke_token(extract_bearer_token(request))
    return {"revoked": revoked}


def _client_ip(request: Request, trusted_proxy_count: int = 0) -> str:
    """The address the rate limiter keys on.

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
