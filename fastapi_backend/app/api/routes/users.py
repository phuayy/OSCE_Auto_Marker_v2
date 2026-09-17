"""``/api/admin/users`` — account administration, administrators only.

The role gate is on the *router*, not on each handler, so a route added here
later is protected by construction; ``tests/test_admin_routes_are_guarded.py``
walks the app and fails if any ``/api/admin/`` path ever lacks it. Every rule
about what may be done to whom lives in ``UserAdminService``; these handlers
only translate HTTP into calls and outcomes into JSON.

Responses carry the full public projection of the account that changed, so
the screen updates one row without a second request — and, as everywhere in
this API, nothing here ever serialises a password hash or a token.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, status

from app.api.dependencies import current_actor, get_container, require_admin
from app.domain.users import UserStatus
from app.schemas.users import InviteUserRequest, UpdateUserRequest
from app.services.container import AppContainer


router = APIRouter(prefix="/admin/users", tags=["users"], dependencies=[Depends(require_admin)])


def _actor(request: Request) -> tuple[str, str]:
    """(user id, label) of the administrator acting — what the service records
    as ``created_by`` and names in the invitation email."""
    actor = current_actor(request)
    return (actor.user_id, actor.label) if actor is not None else ("", "")


@router.get("")
async def list_users(request: Request, container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    """Every account, plus what this deployment can do about email — so the
    screen knows whether an invitation will be sent or has to be copied."""
    actor_id, _ = _actor(request)
    return {
        "users": await container.user_admin.list_users(),
        "me": actor_id,
        "mail": container.user_admin.describe_mail(),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def invite_user(
    payload: InviteUserRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Create an invited account and email it a link.

    The account exists whether or not the email went out: the response says
    which, and a failed delivery is retried with "Resend" rather than by
    inviting again. ``inviteLink`` is present only when this deployment lets
    the admin copy it (no mail relay configured).
    """
    actor_id, actor_name = _actor(request)
    outcome = await container.user_admin.invite(
        email=payload.email,
        role=payload.parsed_role,
        display_name=payload.displayName,
        actor_id=actor_id or None,
        actor_name=actor_name,
    )
    return outcome.to_public()


@router.post("/{user_id}/resend-invite")
async def resend_invite(
    user_id: str, request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    """A fresh link for an account that has not activated yet. The earlier
    link stops working the moment this one exists."""
    actor_id, actor_name = _actor(request)
    outcome = await container.user_admin.resend_invite(user_id, actor_id=actor_id or None, actor_name=actor_name)
    return outcome.to_public()


@router.patch("/{user_id}")
async def update_user(
    user_id: str,
    payload: UpdateUserRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Change the role and/or display name. Demoting the last active
    administrator, or your own account, is refused."""
    actor_id, _ = _actor(request)
    user = await container.user_admin.update_user(
        user_id,
        role=payload.parsed_role,
        display_name=payload.displayName,
        actor_id=actor_id or None,
    )
    return {"user": user}


@router.post("/{user_id}/disable")
async def disable_user(
    user_id: str, request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    """Suspend: refused at login and on every request from now on, including
    the sessions the account holds right now."""
    actor_id, _ = _actor(request)
    user = await container.user_admin.set_status(user_id, UserStatus.DISABLED, actor_id=actor_id or None)
    return {"user": user}


@router.post("/{user_id}/enable")
async def enable_user(
    user_id: str, request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    actor_id, _ = _actor(request)
    user = await container.user_admin.set_status(user_id, UserStatus.ACTIVE, actor_id=actor_id or None)
    return {"user": user}


@router.post("/{user_id}/send-password-reset")
async def send_password_reset(
    user_id: str, request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    """Email an active account a reset link on its holder's behalf."""
    actor_id, _ = _actor(request)
    outcome = await container.user_admin.send_password_reset(user_id, actor_id=actor_id or None)
    payload = outcome.to_public()
    if outcome.link is not None:
        payload["resetLink"] = payload.pop("link")
    return payload


@router.delete("/{user_id}")
async def delete_user(
    user_id: str, request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    """Remove the account and every link issued for it. The last active
    administrator, and your own account, cannot be removed."""
    actor_id, _ = _actor(request)
    await container.user_admin.delete_user(user_id, actor_id=actor_id or None)
    return {"deleted": True}
