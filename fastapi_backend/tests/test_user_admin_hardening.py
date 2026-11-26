"""Account-administration hardening: the last-admin guard under concurrency,
mail kept off the response path, reset requests that cannot void an invitation,
and a bootstrap password held to the password policy."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.security import hash_action_token
from app.domain.users import ActionTokenPurpose, UserRole, UserStatus
from app.mail.base import EmailDeliveryError
from app.services.container import create_container
from app.services.user_admin_service import LastAdminError
from tests.fixtures.mail import RecordingEmailSender
from tests.test_routes import build_test_client
from tests.test_user_admin import GOOD_PASSWORD, MARKER_EMAIL, _accept, _activate_marker, _headers, _invite, _mailer, _token


class GatedEmailSender(RecordingEmailSender):
    """A relay that does not answer until the test lets it."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.attempts = 0
        self.error = ""

    async def send(self, message) -> None:  # noqa: ANN001
        self.attempts += 1
        await self.gate.wait()
        if self.error:
            raise EmailDeliveryError(self.error)
        await super().send(message)


def _swap_mailer(client, mailer) -> None:
    container = client.app.state.container
    container.mailer = mailer
    container.user_admin.mailer = mailer


def _second_admin(client, email: str = "second@example.edu") -> dict:
    admin_token = _token(client)
    second = _activate_marker(client, admin_token, email)
    promoted = client.patch(f"/api/admin/users/{second['id']}", json={"role": "admin"}, headers=_headers(admin_token))
    assert promoted.status_code == 200, promoted.text
    return second


# --- M22: the last-admin guard and the write are one step -------------------------


def _admin_id(client) -> str:
    return client.get("/api/auth/me", headers=_headers(_token(client))).json()["userId"]


@pytest.mark.parametrize("action", ["demote", "disable"])
def test_two_admins_cannot_remove_each_other_concurrently(tmp_path, action) -> None:
    client = build_test_client(tmp_path)
    service = client.app.state.container.user_admin
    first = _admin_id(client)
    second = _second_admin(client)["id"]

    async def remove(target: str, actor: str):
        if action == "demote":
            return await service.update_user(target, role=UserRole.MARKER, actor_id=actor)
        return await service.set_status(target, UserStatus.DISABLED, actor_id=actor)

    async def race():
        return await asyncio.gather(remove(second, first), remove(first, second), return_exceptions=True)

    results = asyncio.run(race())
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(failures) == 1, results
    assert isinstance(failures[0], LastAdminError)
    assert asyncio.run(service.repository.count_active_admins()) == 1


def test_two_admins_cannot_delete_each_other_concurrently(tmp_path) -> None:
    client = build_test_client(tmp_path)
    service = client.app.state.container.user_admin
    first = _admin_id(client)
    second = _second_admin(client)["id"]

    async def race():
        return await asyncio.gather(
            service.delete_user(second, actor_id=first),
            service.delete_user(first, actor_id=second),
            return_exceptions=True,
        )

    results = asyncio.run(race())
    assert sum(isinstance(r, LastAdminError) for r in results) == 1, results
    assert asyncio.run(service.repository.count_active_admins()) == 1


def test_a_non_last_admin_is_still_demotable_and_a_failed_guard_changes_nothing(tmp_path) -> None:
    client = build_test_client(tmp_path)
    service = client.app.state.container.user_admin
    first = _admin_id(client)
    second = _second_admin(client)["id"]

    asyncio.run(service.update_user(second, role=UserRole.MARKER, actor_id=first))
    with pytest.raises(LastAdminError):
        asyncio.run(service.update_user(first, role=UserRole.MARKER, display_name="Renamed", actor_id=second))
    row = asyncio.run(service.repository.get(first))
    assert row.role == UserRole.ADMIN.value and row.display_name != "Renamed"


# --- M23: the fresh token never waits on mail ------------------------------------


def test_change_own_password_answers_before_the_notice_is_delivered(tmp_path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    _activate_marker(client, admin_token)
    other_tab = _token(client, MARKER_EMAIL, GOOD_PASSWORD)
    mailer = GatedEmailSender()
    _swap_mailer(client, mailer)
    service = client.app.state.container.user_admin
    marker = asyncio.run(service.repository.find_by_login(MARKER_EMAIL))

    async def scenario():
        result = await asyncio.wait_for(
            service.change_own_password(marker.id, current_password=GOOD_PASSWORD, new_password="brand-new-password-1"),
            timeout=5,
        )
        assert result["token"]
        assert mailer.sent == []
        mailer.gate.set()
        await service.drain()
        return result

    asyncio.run(scenario())
    assert mailer.last.to == MARKER_EMAIL and "changed" in mailer.last.subject
    # Other sessions are still revoked.
    assert client.get("/api/sessions", headers=_headers(other_tab)).status_code == 401


def test_a_failing_relay_does_not_fail_the_password_change(tmp_path, caplog) -> None:
    client = build_test_client(tmp_path)
    _activate_marker(client, _token(client))
    mailer = GatedEmailSender()
    mailer.error = "relay down"
    mailer.gate.set()
    _swap_mailer(client, mailer)
    service = client.app.state.container.user_admin
    marker = asyncio.run(service.repository.find_by_login(MARKER_EMAIL))

    async def scenario():
        with caplog.at_level(logging.WARNING):
            result = await service.change_own_password(
                marker.id, current_password=GOOD_PASSWORD, new_password="brand-new-password-1"
            )
            await service.drain()
        return result

    assert asyncio.run(scenario())["token"]
    assert "relay down" in caplog.text


# --- M24: a reset request does the same visible work for every identifier ---------


def test_reset_request_never_waits_on_mail_for_a_known_or_unknown_identifier(tmp_path) -> None:
    client = build_test_client(tmp_path)
    _activate_marker(client, _token(client))
    mailer = GatedEmailSender()
    _swap_mailer(client, mailer)
    service = client.app.state.container.user_admin

    async def scenario():
        for identifier in ("nobody@example.edu", MARKER_EMAIL):
            await asyncio.wait_for(service.request_password_reset(identifier), timeout=5)
        assert mailer.sent == []
        mailer.gate.set()
        await service.drain()

    asyncio.run(scenario())
    assert [m.to for m in mailer.sent] == [MARKER_EMAIL]
    assert mailer.last_link().startswith("http://localhost:5173/#/reset-password/")


def test_a_reset_mail_failure_is_logged_not_raised(tmp_path, caplog) -> None:
    client = build_test_client(tmp_path)
    _activate_marker(client, _token(client))
    mailer = GatedEmailSender()
    mailer.error = "relay down"
    mailer.gate.set()
    _swap_mailer(client, mailer)
    service = client.app.state.container.user_admin

    async def scenario():
        with caplog.at_level(logging.WARNING):
            await service.request_password_reset(MARKER_EMAIL)
            await service.drain()

    asyncio.run(scenario())
    assert "relay down" in caplog.text


def test_the_reset_endpoint_still_answers_identically_and_throttles(tmp_path) -> None:
    client = build_test_client(tmp_path)
    _activate_marker(client, _token(client))
    known = client.post("/api/auth/password-reset/request", json={"identifier": MARKER_EMAIL})
    unknown = client.post("/api/auth/password-reset/request", json={"identifier": "nobody@example.edu"})
    assert known.status_code == unknown.status_code == 200 and known.json() == unknown.json()
    limit = client.app.state.container.settings.token_rate_limit_max_attempts
    statuses = [
        client.post("/api/auth/password-reset/request", json={"identifier": "x@example.edu"}).status_code
        for _ in range(limit)
    ]
    assert statuses[-1] == 429


# --- M25: a reset request cannot void someone's invitation -----------------------


def test_a_reset_request_for_an_invited_account_leaves_the_invitation_alone(tmp_path) -> None:
    client = build_test_client(tmp_path)
    mailer = _mailer(client)
    _invite(client, _token(client))
    invite_token = mailer.last_token()
    sent = len(mailer.sent)
    service = client.app.state.container.user_admin

    async def request_and_settle():
        await service.request_password_reset(MARKER_EMAIL)
        await service.drain()

    asyncio.run(request_and_settle())

    assert len(mailer.sent) == sent
    token_hash = hash_action_token(invite_token)
    pending = asyncio.run(service.repository.pending_token(
        asyncio.run(service.repository.find_by_login(MARKER_EMAIL)).id, ActionTokenPurpose.INVITE
    ))
    assert pending is not None and pending.token_hash == token_hash
    assert _accept(client, invite_token).status_code == 200


def test_a_new_reset_request_voids_only_the_earlier_reset_link(tmp_path) -> None:
    client = build_test_client(tmp_path)
    mailer = _mailer(client)
    _activate_marker(client, _token(client))
    service = client.app.state.container.user_admin

    async def request_twice() -> list[str]:
        links = []
        for _ in range(2):
            await service.request_password_reset(MARKER_EMAIL)
            await service.drain()
            links.append(mailer.last_token())
        return links

    first, second = asyncio.run(request_twice())
    assert first != second
    assert client.get(f"/api/auth/password-reset/{first}").json()["reason"] == "used"
    assert client.get(f"/api/auth/password-reset/{second}").json()["valid"] is True


def test_resending_an_invite_still_voids_the_previous_invite(tmp_path) -> None:
    client = build_test_client(tmp_path)
    mailer = _mailer(client)
    admin_token = _token(client)
    created = _invite(client, admin_token)
    first = mailer.last_token()
    assert client.post(f"/api/admin/users/{created.json()['user']['id']}/resend-invite", headers=_headers(admin_token)).status_code == 200
    assert client.get(f"/api/auth/invitations/{first}").json()["valid"] is False
    assert client.get(f"/api/auth/invitations/{mailer.last_token()}").json()["valid"] is True


def test_an_issued_reset_token_does_not_void_a_live_invite_at_the_repository(tmp_path) -> None:
    client = build_test_client(tmp_path)
    repository = client.app.state.container.users
    _invite(client, _token(client))
    user = asyncio.run(repository.find_by_login(MARKER_EMAIL))
    from datetime import timedelta
    from app.database.models import utc_now

    asyncio.run(
        repository.issue_token(
            user_id=user.id,
            purpose=ActionTokenPurpose.PASSWORD_RESET,
            token_hash="x" * 64,
            expires_at=utc_now() + timedelta(minutes=5),
        )
    )
    assert asyncio.run(repository.pending_token(user.id, ActionTokenPurpose.INVITE)) is not None


# --- M44: the bootstrap password is held to the policy ---------------------------


def _bootstrap(tmp_path: Path, password: str, *, environment: str):
    settings = Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        auth_bcrypt_rounds=4,
        default_admin_password=password,
        environment=environment,
        app_database_url="",
        database_url="",
    )
    container = create_container(settings, mailer=RecordingEmailSender())
    asyncio.run(container.artifacts.ensure_storage_layout())
    asyncio.run(container.auth.initialize())
    return container, lambda: asyncio.run(container.user_admin.ensure_bootstrap_admin())


@pytest.mark.parametrize("password", ["change-me", "<generate: a strong first-boot password>", "admin", "ChangeMe-1234567", "My-Password-12345"])
def test_production_refuses_a_placeholder_or_weak_bootstrap_password(tmp_path, password) -> None:
    container, bootstrap = _bootstrap(tmp_path, password, environment="production")
    with pytest.raises(RuntimeError, match="DEFAULT_ADMIN_PASSWORD"):
        bootstrap()
    assert asyncio.run(container.users.count()) == 0


def test_development_warns_and_still_seeds_the_admin(tmp_path, caplog) -> None:
    container, bootstrap = _bootstrap(tmp_path, "change-me", environment="development")
    with caplog.at_level(logging.WARNING):
        outcome = bootstrap()
    assert outcome.action == "created"
    assert "DEFAULT_ADMIN_PASSWORD" in caplog.text


def test_a_strong_bootstrap_password_is_fine_in_production(tmp_path, caplog) -> None:
    container, bootstrap = _bootstrap(tmp_path, "k7Vq-2mXw9-Zt4Rb-pL8s", environment="production")
    with caplog.at_level(logging.WARNING):
        assert bootstrap().action == "created"
    assert "DEFAULT_ADMIN_PASSWORD" not in caplog.text


def test_existing_accounts_skip_the_bootstrap_password_check(tmp_path) -> None:
    container, bootstrap = _bootstrap(tmp_path, "k7Vq-2mXw9-Zt4Rb-pL8s", environment="production")
    bootstrap()
    container.user_admin.settings = replace(container.settings, default_admin_password="change-me")
    assert bootstrap().action == "existing"
