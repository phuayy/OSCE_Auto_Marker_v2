from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.dependencies import client_ip, extract_bearer_token, get_auth_payload, get_container
from app.domain.users import ActionTokenPurpose
from app.schemas.auth import (
    AuthMeResponse,
    AuthResponse,
    LoginRequest,
    LogoutResponse,
    StreamTicketResponse,
)
from app.schemas.users import (
    AcceptInvitationRequest,
    ChangePasswordRequest,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
)
from app.services.container import AppContainer


router = APIRouter(prefix="/auth", tags=["auth"])


# --- sessions -------------------------------------------------------------------


@router.post("/login", response_model=AuthResponse)
async def login(
    request: Request,
    payload: LoginRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    container.login_rate_limiter.check(client_ip(request, container.settings.trusted_proxy_count))
    result = await container.auth.authenticate(payload.username, payload.password)
    if not result:
        # One message for every refusal — unknown account, not yet activated,
        # disabled, wrong password — so the endpoint cannot enumerate accounts.
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    return result


@router.get("/me", response_model=AuthMeResponse)
async def me(request: Request, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    token = extract_bearer_token(request)
    payload = await container.auth.verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return {
        "userId": payload["sub"],
        "username": payload["username"],
        "role": payload["role"],
        "displayName": payload.get("displayName") or "",
        "email": payload.get("email") or "",
        "expiresAt": payload["expiresAt"],
    }


@router.get("/stream-ticket", response_model=StreamTicketResponse)
async def stream_ticket(request: Request, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    """Mint a short-lived ticket for SSE/media URLs (requires a valid bearer token)."""
    payload = get_auth_payload(request)
    if not payload:
        raise HTTPException(status_code=401, detail="Authentication required.")
    ticket = await container.auth.issue_stream_ticket(str(payload["sub"]))
    if ticket is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return ticket


@router.post("/logout", response_model=LogoutResponse)
async def logout(request: Request, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    """Server-side logout: revoke the presented token so it cannot be reused."""
    revoked = await container.auth.revoke_token(extract_bearer_token(request))
    return {"revoked": revoked}


@router.post("/password", response_model=AuthResponse)
async def change_password(
    request: Request,
    payload: ChangePasswordRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    """Change the signed-in account's own password.

    Every other session the account holds ends (the token version moves); the
    response carries a fresh token so *this* one continues without a new
    sign-in.
    """
    actor = get_auth_payload(request)
    if not actor:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return await container.user_admin.change_own_password(
        str(actor["sub"]),
        current_password=payload.currentPassword,
        new_password=payload.newPassword,
    )


# --- emailed links ----------------------------------------------------------------
#
# Reachable without a session (see OPEN_API_PREFIXES): the person following an
# invitation has no account yet, and the one resetting a password has lost the
# way into theirs. The token in the URL is the credential, and a per-IP throttle
# stands in front of every one of these.


def _throttle(request: Request, container: AppContainer) -> None:
    container.token_rate_limiter.check(client_ip(request, container.settings.trusted_proxy_count))


@router.get("/invitations/{token}")
async def describe_invitation(
    token: str, request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, object]:
    """Whose invitation this is and whether it still works — what the screen
    shows before asking for a password. Always 200: an unusable link is an
    answer, not an error."""
    _throttle(request, container)
    description = await container.user_admin.describe_token(token, ActionTokenPurpose.INVITE)
    return description.to_public()


@router.post("/invitations/{token}/accept")
async def accept_invitation(
    token: str,
    payload: AcceptInvitationRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    """Set the first password and activate the account. No session is issued:
    the person signs in normally next, which is also how the screen confirms
    the password they chose actually works."""
    _throttle(request, container)
    user = await container.user_admin.accept_invitation(
        token, password=payload.password, display_name=payload.displayName
    )
    return {"ok": True, "username": user["username"], "email": user["email"]}


@router.post("/password-reset/request")
async def request_password_reset(
    payload: PasswordResetRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    """Always the same answer, whatever the identifier names. If it names an
    account with an address, that mailbox gets a link."""
    _throttle(request, container)
    await container.user_admin.request_password_reset(payload.identifier)
    return {"ok": True, "message": "If that account exists, an email with a reset link is on its way."}


@router.get("/password-reset/{token}")
async def describe_password_reset(
    token: str, request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, object]:
    _throttle(request, container)
    description = await container.user_admin.describe_token(token, ActionTokenPurpose.PASSWORD_RESET)
    return description.to_public()


@router.post("/password-reset/{token}/confirm")
async def confirm_password_reset(
    token: str,
    payload: PasswordResetConfirmRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    _throttle(request, container)
    user = await container.user_admin.reset_password(token, password=payload.password)
    return {"ok": True, "username": user["username"]}
