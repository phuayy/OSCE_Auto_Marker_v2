"""Account administration: invitations, roles, suspension, password recovery.

Everything an administrator does to accounts, and everything an account holder
does to their own, goes through here. The rules are the interesting part, and
they are all in one place so the routes stay thin:

* **The last active admin is untouchable.** They cannot be demoted, disabled or
  deleted, because the next action would need an admin and there would be none.
* **Nobody changes their own access.** An admin cannot disable, delete or
  demote themselves — a slip of the mouse would lock them out mid-session.
  Their own password is the one thing they may change.
* **An emailed link is a capability, and a spent one is worthless.** Tokens are
  minted from a CSPRNG, stored as a hash, single-use by an atomic update,
  voided by a resend, and judged against the *account's* state as well as
  their own expiry — an invitation cannot activate an account an admin has
  since disabled.
* **Every change to an account's access bumps its token version.** Activation,
  a password change, a reset and a suspension all revoke the bearer tokens the
  account already holds; verification compares the version on every request.
* **Public endpoints never say whether an account exists.** A password-reset
  request for an unknown address is answered exactly like one for a known
  address, and the mail failure — if any — goes to the log, not the caller.

Delivery is best-effort at the *request* level: an invitation whose email
bounced is still an invited account, reported as such, with "Resend" and (when
no relay is configured) "Copy link" as the ways forward. Rolling the account
back would turn a mail-server hiccup into a lost onboarding.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.json_utils import read_json_file
from app.core.security import hash_action_token, hash_password, new_action_token, verify_password
from app.database.models import UserActionTokenRecord, UserRecord, utc_now
from app.domain.users import (
    ActionTokenPurpose,
    UserRole,
    UserStatus,
    UserVocabularyError,
    normalize_display_name,
    normalize_email,
    normalize_login_identifier,
    normalize_username,
    password_policy_errors,
)
from app.mail import EmailDeliveryError, EmailSender, templates
from app.mail.links import invite_link, password_reset_link
from app.repositories.user_repository import UserRepository, as_utc
from app.services.auth_service import AuthService
from app.services.user_directory import UserDirectory


logger = logging.getLogger(__name__)


# --- errors --------------------------------------------------------------------


class UserAdminError(AppError):
    """A refusal the caller can act on; the status code says which kind."""


class UserNotFoundError(UserAdminError):
    def __init__(self, user_id: str) -> None:
        super().__init__(f"No account with id '{user_id}'.", status_code=404)


class UserConflictError(UserAdminError):
    """The action does not fit the account's current state."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=409)


class LastAdminError(UserAdminError):
    def __init__(self, action: str) -> None:
        super().__init__(
            f"Cannot {action} the last active administrator. Promote another account first.",
            status_code=409,
        )


class SelfModificationError(UserAdminError):
    def __init__(self, action: str) -> None:
        super().__init__(
            f"You cannot {action} your own account. Ask another administrator.",
            status_code=400,
        )


class PasswordPolicyError(UserAdminError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__(" ".join(errors), status_code=422)
        self.errors = list(errors)


class InvalidActionTokenError(UserAdminError):
    """The link cannot be followed. ``reason`` is one of the ``TOKEN_*`` codes
    below and is what the screen renders its explanation from."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message, status_code=410)
        self.reason = reason


TOKEN_INVALID = "invalid"
TOKEN_EXPIRED = "expired"
TOKEN_USED = "used"
TOKEN_ACCOUNT_UNAVAILABLE = "account_unavailable"

_TOKEN_MESSAGES = {
    TOKEN_INVALID: "This link is not valid.",
    TOKEN_EXPIRED: "This link has expired. Ask an administrator to send a new one.",
    TOKEN_USED: "This link has already been used.",
    TOKEN_ACCOUNT_UNAVAILABLE: "This account is no longer available. Contact an administrator.",
}


# --- outcomes ------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryOutcome:
    """What happened to the email, and — when the deployment allows it — the
    link itself so an admin can hand it over by other means."""

    sent: bool
    error: str = ""
    link: str | None = None

    def to_public(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"mailSent": self.sent, "mailError": self.error}
        if self.link is not None:
            payload["link"] = self.link
        return payload


@dataclass(frozen=True)
class InviteOutcome:
    user: dict[str, Any]
    delivery: DeliveryOutcome

    def to_public(self) -> dict[str, Any]:
        payload = {"user": self.user, **self.delivery.to_public()}
        if self.delivery.link is not None:
            payload["inviteLink"] = self.delivery.link
        payload.pop("link", None)
        return payload


@dataclass(frozen=True)
class BootstrapOutcome:
    action: str  # existing | migrated | created
    username: str = ""


@dataclass
class TokenDescription:
    valid: bool
    reason: str = ""
    message: str = ""
    email: str = ""
    display_name: str = ""
    expires_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_public(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "reason": self.reason,
            "message": self.message,
            "email": self.email,
            "displayName": self.display_name,
            "expiresAt": self.expires_at,
            **self.extra,
        }


# --- the service --------------------------------------------------------------


class UserAdminService:
    def __init__(
        self,
        settings: Settings,
        repository: UserRepository,
        directory: UserDirectory,
        auth: AuthService,
        mailer: EmailSender,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.directory = directory
        self.auth = auth
        self.mailer = mailer
        self._clock = clock

    # --- startup ---------------------------------------------------------------

    async def startup(self) -> BootstrapOutcome:
        """API-role startup: make sure someone can sign in, and sweep old tokens."""
        outcome = await self.ensure_bootstrap_admin()
        try:
            purged = await self.repository.purge_spent_tokens(older_than=self._clock() - timedelta(days=30))
            if purged:
                logger.info("Purged %d spent account tokens.", purged)
        except Exception:  # noqa: BLE001 - housekeeping must never block a boot
            logger.warning("Could not purge spent account tokens.", exc_info=True)
        return outcome

    async def ensure_bootstrap_admin(self) -> BootstrapOutcome:
        """Seed the first administrator when the table is empty.

        Preference order: the legacy ``storage/auth/credentials.json`` (so an
        existing deployment keeps the password it has), then
        ``DEFAULT_ADMIN_USERNAME`` / ``DEFAULT_ADMIN_PASSWORD``. Nothing is
        seeded once any account exists — an admin who deleted the bootstrap
        account on purpose must not find it back after a restart.
        """
        if await self.repository.count() > 0:
            return BootstrapOutcome(action="existing")

        email = self.settings.default_admin_email or None
        if email:
            try:
                email = normalize_email(email)
            except UserVocabularyError as error:
                raise RuntimeError(f"DEFAULT_ADMIN_EMAIL is not usable: {error}") from error

        legacy = await asyncio.to_thread(read_json_file, self.settings.paths.credentials_path)
        if (
            legacy
            and isinstance(legacy.get("username"), str)
            and isinstance(legacy.get("passwordHash"), str)
            and legacy["passwordHash"].strip()
        ):
            try:
                username = normalize_username(legacy["username"])
            except UserVocabularyError as error:
                raise RuntimeError(
                    f"The username in {self.settings.paths.credentials_path} cannot be migrated: {error}"
                ) from error
            await self.repository.create(
                username=username,
                email=email,
                display_name=normalize_display_name(legacy.get("displayName") or ""),
                role=UserRole.ADMIN,
                status=UserStatus.ACTIVE,
                password_hash=legacy["passwordHash"].strip(),
            )
            self.directory.invalidate("bootstrap admin migrated")
            logger.info("Migrated the administrator account '%s' from credentials.json.", username)
            return BootstrapOutcome(action="migrated", username=username)

        if not self.settings.default_admin_password:
            raise RuntimeError(
                "DEFAULT_ADMIN_PASSWORD is required for first boot when no account exists yet "
                "(and storage/auth/credentials.json is missing). Set it in .env or platform secrets."
            )
        username = normalize_username(self.settings.default_admin_username)
        password_hash = await asyncio.to_thread(
            hash_password, self.settings.default_admin_password, self.settings.auth_bcrypt_rounds
        )
        await self.repository.create(
            username=username,
            email=email,
            display_name="",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            password_hash=password_hash,
        )
        self.directory.invalidate("bootstrap admin created")
        logger.info("Created the administrator account '%s' from DEFAULT_ADMIN_*.", username)
        return BootstrapOutcome(action="created", username=username)

    # --- reads -----------------------------------------------------------------

    def invite_link_visible(self) -> bool:
        """Whether the admin screen may be handed the raw link.

        Follows the mail backend unless overridden: when nothing can deliver
        the email the link is the only way an invitation reaches anyone, and
        the admin is trusted with every other account action already.
        """
        override = self.settings.invite_link_visible_to_admin
        if override in {"true", "1", "yes", "on"}:
            return True
        if override in {"false", "0", "no", "off"}:
            return False
        return not self.mailer.configured

    def describe_mail(self) -> dict[str, Any]:
        return {
            "backend": self.mailer.backend,
            "configured": bool(self.mailer.configured),
            "inviteLinkVisible": self.invite_link_visible(),
        }

    async def list_users(self) -> list[dict[str, Any]]:
        records = await self.repository.list_all()
        pending = await self.repository.list_pending_tokens(ActionTokenPurpose.INVITE)
        return [self.repository.to_public(record, invite=pending.get(record.id)) for record in records]

    async def get_user(self, user_id: str) -> dict[str, Any]:
        record = await self._require(user_id)
        invite = await self.repository.pending_token(record.id, ActionTokenPurpose.INVITE)
        return self.repository.to_public(record, invite=invite)

    # --- invitations -----------------------------------------------------------

    async def invite(
        self,
        *,
        email: str,
        role: UserRole = UserRole.MARKER,
        display_name: str = "",
        actor_id: str | None = None,
        actor_name: str = "",
    ) -> InviteOutcome:
        address = normalize_email(email)
        if await self.repository.find_by_login(address) is not None:
            raise UserConflictError(f"An account for {address} already exists.")
        record = await self.repository.create(
            username=address,
            email=address,
            display_name=normalize_display_name(display_name),
            role=role,
            status=UserStatus.INVITED,
            password_hash=None,
            created_by=actor_id,
        )
        self.directory.invalidate("account invited")
        delivery = await self._send_invitation(record, actor_id=actor_id, actor_name=actor_name)
        invite = await self.repository.pending_token(record.id, ActionTokenPurpose.INVITE)
        return InviteOutcome(user=self.repository.to_public(record, invite=invite), delivery=delivery)

    async def resend_invite(self, user_id: str, *, actor_id: str | None = None, actor_name: str = "") -> InviteOutcome:
        record = await self._require(user_id)
        if record.status != UserStatus.INVITED.value:
            raise UserConflictError("Only an account that has not yet been activated can be re-invited.")
        delivery = await self._send_invitation(record, actor_id=actor_id, actor_name=actor_name)
        invite = await self.repository.pending_token(record.id, ActionTokenPurpose.INVITE)
        return InviteOutcome(user=self.repository.to_public(record, invite=invite), delivery=delivery)

    async def _send_invitation(self, record: UserRecord, *, actor_id: str | None, actor_name: str) -> DeliveryOutcome:
        if not record.email:
            raise UserConflictError("This account has no email address to invite.")
        raw = new_action_token()
        expires_at = self._clock() + timedelta(hours=self.settings.invite_token_ttl_hours)
        await self.repository.issue_token(
            user_id=record.id,
            purpose=ActionTokenPurpose.INVITE,
            token_hash=hash_action_token(raw),
            expires_at=expires_at,
            created_by=actor_id,
        )
        link = invite_link(self.settings.app_public_url, raw)
        message = templates.invitation(
            to=record.email,
            display_name=record.display_name or "",
            invited_by=actor_name,
            link=link,
            expires_hours=self.settings.invite_token_ttl_hours,
        )
        return await self._deliver(message, link=link)

    async def _deliver(self, message: Any, *, link: str) -> DeliveryOutcome:
        visible_link = link if self.invite_link_visible() else None
        try:
            await self.mailer.send(message)
        except EmailDeliveryError as error:
            return DeliveryOutcome(sent=False, error=error.message, link=visible_link)
        return DeliveryOutcome(sent=bool(self.mailer.configured), link=visible_link)

    # --- account changes -------------------------------------------------------

    async def update_user(
        self,
        user_id: str,
        *,
        role: UserRole | None = None,
        display_name: str | None = None,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        record = await self._require(user_id)
        if role is not None and role.value != record.role and role is not UserRole.ADMIN:
            # The last-admin guard runs first on purpose: a sole administrator
            # demoting themselves should hear "promote someone else first",
            # not "ask another administrator" when there is none.
            await self._guard_last_admin(record, "demote")
            if actor_id and actor_id == record.id:
                raise SelfModificationError("change the role of")

        def mutate(row: UserRecord) -> None:
            if role is not None:
                row.role = role.value
            if display_name is not None:
                row.display_name = normalize_display_name(display_name)

        updated = await self.repository.update(record.id, mutate)
        self.directory.invalidate("account updated")
        assert updated is not None
        invite = await self.repository.pending_token(updated.id, ActionTokenPurpose.INVITE)
        return self.repository.to_public(updated, invite=invite)

    async def set_status(self, user_id: str, status: UserStatus, *, actor_id: str | None = None) -> dict[str, Any]:
        """Suspend (``disabled``) or reinstate (``active``) an account.

        Suspension bumps the token version so every session the account holds
        ends on its next request. Reinstating an account that was only ever
        invited is not a thing — it has no password — so that transition is
        refused; re-invite it instead.
        """
        record = await self._require(user_id)
        if status not in (UserStatus.ACTIVE, UserStatus.DISABLED):
            raise UserConflictError("An account can only be enabled or disabled here.")
        if status.value == record.status:
            return self.repository.to_public(record)
        if status is UserStatus.DISABLED:
            await self._guard_last_admin(record, "disable")
            if actor_id and actor_id == record.id:
                raise SelfModificationError("disable")
        if status is UserStatus.ACTIVE and not record.password_hash:
            raise UserConflictError("This account has not set a password yet; re-send its invitation instead.")

        def mutate(row: UserRecord) -> None:
            row.status = status.value
            if status is UserStatus.DISABLED:
                row.token_version = int(row.token_version or 0) + 1

        updated = await self.repository.update(record.id, mutate)
        self.directory.invalidate(f"account {status.value}")
        assert updated is not None
        return self.repository.to_public(updated)

    async def delete_user(self, user_id: str, *, actor_id: str | None = None) -> None:
        record = await self._require(user_id)
        await self._guard_last_admin(record, "delete")
        if actor_id and actor_id == record.id:
            raise SelfModificationError("delete")
        await self.repository.delete(record.id)
        self.directory.invalidate("account deleted")

    async def _guard_last_admin(self, record: UserRecord, action: str) -> None:
        if record.role != UserRole.ADMIN.value or record.status != UserStatus.ACTIVE.value:
            return
        if await self.repository.count_active_admins() <= 1:
            raise LastAdminError(action)

    async def _require(self, user_id: str) -> UserRecord:
        record = await self.repository.get(user_id)
        if record is None:
            raise UserNotFoundError(str(user_id))
        return record

    # --- password reset ----------------------------------------------------------

    async def send_password_reset(self, user_id: str, *, actor_id: str | None = None) -> DeliveryOutcome:
        """Admin-triggered: a reset link for an active account that has an address."""
        record = await self._require(user_id)
        if record.status != UserStatus.ACTIVE.value:
            raise UserConflictError("Only an active account can be sent a password reset.")
        if not record.email:
            raise UserConflictError("This account has no email address to send a reset to.")
        return await self._send_reset(record, actor_id=actor_id)

    async def request_password_reset(self, identifier: str) -> None:
        """Public: whoever holds the mailbox gets a link; everyone else learns nothing.

        An invited-but-never-activated account is sent its invitation again —
        it has no password to reset — and an unknown or disabled account is
        silently ignored. The caller answers the same way in every case.
        """
        record = await self.repository.find_by_login(normalize_login_identifier(identifier))
        if record is None or not record.email:
            return
        try:
            if record.status == UserStatus.ACTIVE.value:
                outcome = await self._send_reset(record, actor_id=None)
            elif record.status == UserStatus.INVITED.value:
                outcome = await self._send_invitation(record, actor_id=None, actor_name="")
            else:
                return
        except UserAdminError:
            return
        if not outcome.sent and outcome.error:
            logger.warning("Password-reset email to %s failed: %s", record.email, outcome.error)

    async def _send_reset(self, record: UserRecord, *, actor_id: str | None) -> DeliveryOutcome:
        raw = new_action_token()
        expires_at = self._clock() + timedelta(minutes=self.settings.password_reset_token_ttl_minutes)
        await self.repository.issue_token(
            user_id=record.id,
            purpose=ActionTokenPurpose.PASSWORD_RESET,
            token_hash=hash_action_token(raw),
            expires_at=expires_at,
            created_by=actor_id,
        )
        link = password_reset_link(self.settings.app_public_url, raw)
        message = templates.password_reset(
            to=str(record.email),
            display_name=record.display_name or "",
            link=link,
            expires_minutes=self.settings.password_reset_token_ttl_minutes,
        )
        return await self._deliver(message, link=link)

    # --- following a link --------------------------------------------------------

    async def describe_token(self, raw_token: str, purpose: ActionTokenPurpose) -> TokenDescription:
        """What the screen shows before asking for a password: whose account,
        and whether the link still works — with the reason when it does not."""
        found = await self.repository.peek_token(hash_action_token(raw_token), purpose)
        if found is None:
            return self._invalid(TOKEN_INVALID)
        token, user = found
        reason = self._token_problem(token, user, purpose)
        if reason:
            return self._invalid(reason)
        return TokenDescription(
            valid=True,
            email=user.email or "",
            display_name=user.display_name or "",
            expires_at=(as_utc(token.expires_at) or self._clock()).isoformat().replace("+00:00", "Z"),
        )

    @staticmethod
    def _invalid(reason: str) -> TokenDescription:
        return TokenDescription(valid=False, reason=reason, message=_TOKEN_MESSAGES[reason])

    def _token_problem(self, token: UserActionTokenRecord, user: UserRecord, purpose: ActionTokenPurpose) -> str:
        if token.used_at is not None:
            return TOKEN_USED
        expires_at = as_utc(token.expires_at)
        if expires_at is None or expires_at <= self._clock():
            return TOKEN_EXPIRED
        if purpose is ActionTokenPurpose.INVITE and user.status != UserStatus.INVITED.value:
            return TOKEN_ACCOUNT_UNAVAILABLE
        if purpose is ActionTokenPurpose.PASSWORD_RESET and user.status != UserStatus.ACTIVE.value:
            return TOKEN_ACCOUNT_UNAVAILABLE
        return ""

    async def accept_invitation(self, raw_token: str, *, password: str, display_name: str | None = None) -> dict[str, Any]:
        """Set the first password and activate the account. The policy is
        checked *before* the token is consumed, so a weak first attempt does
        not burn the link."""
        token, user = await self._checked_token(raw_token, ActionTokenPurpose.INVITE)
        self._check_password(password, user)
        await self._consume(token, ActionTokenPurpose.INVITE)
        password_hash = await asyncio.to_thread(hash_password, password, self.settings.auth_bcrypt_rounds)
        name = normalize_display_name(display_name) if display_name is not None else None

        def mutate(row: UserRecord) -> None:
            row.password_hash = password_hash
            row.status = UserStatus.ACTIVE.value
            row.token_version = int(row.token_version or 0) + 1
            if name is not None:
                row.display_name = name

        updated = await self.repository.update(user.id, mutate)
        self.directory.invalidate("invitation accepted")
        assert updated is not None
        return self.repository.to_public(updated)

    async def reset_password(self, raw_token: str, *, password: str) -> dict[str, Any]:
        token, user = await self._checked_token(raw_token, ActionTokenPurpose.PASSWORD_RESET)
        self._check_password(password, user)
        await self._consume(token, ActionTokenPurpose.PASSWORD_RESET)
        password_hash = await asyncio.to_thread(hash_password, password, self.settings.auth_bcrypt_rounds)

        def mutate(row: UserRecord) -> None:
            row.password_hash = password_hash
            row.token_version = int(row.token_version or 0) + 1

        updated = await self.repository.update(user.id, mutate)
        self.directory.invalidate("password reset")
        assert updated is not None
        await self._notify_password_changed(updated)
        return self.repository.to_public(updated)

    async def change_own_password(self, user_id: str, *, current_password: str, new_password: str) -> dict[str, Any]:
        """Verify the current password, set the new one, end every other
        session, and answer with a fresh token so this one continues."""
        record = await self._require(user_id)
        if record.status != UserStatus.ACTIVE.value or not record.password_hash:
            raise UserConflictError("This account cannot change its password right now.")
        if not await asyncio.to_thread(verify_password, current_password, record.password_hash):
            raise UserAdminError("The current password is not correct.", status_code=400)
        self._check_password(new_password, record)
        password_hash = await asyncio.to_thread(hash_password, new_password, self.settings.auth_bcrypt_rounds)

        def mutate(row: UserRecord) -> None:
            row.password_hash = password_hash
            row.token_version = int(row.token_version or 0) + 1

        await self.repository.update(record.id, mutate)
        self.directory.invalidate("password changed")
        snapshot = await self.directory.get(record.id)
        assert snapshot is not None
        await self._notify_password_changed(record)
        return self.auth.issue_session_token(snapshot)

    async def _checked_token(self, raw_token: str, purpose: ActionTokenPurpose) -> tuple[UserActionTokenRecord, UserRecord]:
        found = await self.repository.peek_token(hash_action_token(raw_token), purpose)
        if found is None:
            raise InvalidActionTokenError(TOKEN_INVALID, _TOKEN_MESSAGES[TOKEN_INVALID])
        token, user = found
        reason = self._token_problem(token, user, purpose)
        if reason:
            raise InvalidActionTokenError(reason, _TOKEN_MESSAGES[reason])
        return token, user

    async def _consume(self, token: UserActionTokenRecord, purpose: ActionTokenPurpose) -> None:
        consumed = await self.repository.consume_token(token.token_hash, purpose)
        if consumed is None:
            # Lost the race with another click on the same link.
            raise InvalidActionTokenError(TOKEN_USED, _TOKEN_MESSAGES[TOKEN_USED])

    @staticmethod
    def _check_password(password: str, user: UserRecord) -> None:
        local_part = (user.email or "").split("@", 1)[0]
        errors = password_policy_errors(password, forbidden=(user.username, user.email or "", local_part))
        if errors:
            raise PasswordPolicyError(errors)

    async def _notify_password_changed(self, record: UserRecord) -> None:
        if not record.email:
            return
        try:
            await self.mailer.send(
                templates.password_changed(to=record.email, display_name=record.display_name or "")
            )
        except EmailDeliveryError as error:
            logger.warning("Password-changed notice to %s failed: %s", record.email, error.message)
