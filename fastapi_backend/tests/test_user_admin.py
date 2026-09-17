"""Accounts: invitations, roles, suspension, and password recovery, end to end.

Every scenario goes through the HTTP surface the browser uses, with the mail
backend replaced by a recorder so a test can follow the link an email carried
the way a person would. The rules under test are the ones ``UserAdminService``
documents: the last admin is untouchable, nobody changes their own access, a
link is single-use and judged against the account's state, and every change
to an account's access ends the sessions it already holds.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from app.core.security import hash_password
from app.database.models import utc_now
from app.domain.users import ActionTokenPurpose, UserRole, UserStatus
from tests.test_routes import build_test_client


ADMIN = {"username": "admin", "password": "admin"}
MARKER_EMAIL = "marker@example.edu"
GOOD_PASSWORD = "correct-horse-battery"


def _login(client, username: str = "admin", password: str = "admin"):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def _token(client, username: str = "admin", password: str = "admin") -> str:
    response = _login(client, username, password)
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mailer(client):
    return client.app.state.container.mailer


def _invite(client, admin_token: str, email: str = MARKER_EMAIL, **extra):
    return client.post(
        "/api/admin/users",
        json={"email": email, "role": "marker", **extra},
        headers=_headers(admin_token),
    )


def _accept(client, token: str, password: str = GOOD_PASSWORD, **extra):
    return client.post(f"/api/auth/invitations/{token}/accept", json={"password": password, **extra})


def _activate_marker(client, admin_token: str, email: str = MARKER_EMAIL) -> dict:
    """Invite + accept, returning the created account's public projection."""
    created = _invite(client, admin_token, email)
    assert created.status_code == 201, created.text
    accepted = _accept(client, _mailer(client).last_token())
    assert accepted.status_code == 200, accepted.text
    return created.json()["user"]


# --- bootstrap ------------------------------------------------------------------


def test_bootstrap_admin_is_created_from_the_default_password(tmp_path) -> None:
    client = build_test_client(tmp_path)
    me = client.get("/api/auth/me", headers=_headers(_token(client)))
    assert me.status_code == 200
    body = me.json()
    assert body["username"] == "admin"
    assert body["role"] == UserRole.ADMIN.value
    assert "passwordHash" not in body and "token_version" not in body


def test_legacy_credentials_file_seeds_the_admin_with_its_existing_hash(tmp_path) -> None:
    """An upgraded deployment keeps the password it already has."""
    auth_dir = tmp_path / "storage" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "credentials.json").write_text(
        json.dumps({"username": "Examiner", "passwordHash": hash_password("legacy-secret-99", 4)}),
        encoding="utf-8",
    )
    client = build_test_client(tmp_path)

    # The username was lowercased on migration; the old password still works.
    assert _login(client, "examiner", "legacy-secret-99").status_code == 200
    # The DEFAULT_ADMIN_PASSWORD the test client configures was *not* used.
    assert _login(client, "admin", "admin").status_code == 401


def test_bootstrap_does_not_resurrect_a_deleted_admin(tmp_path) -> None:
    client = build_test_client(tmp_path)
    container = client.app.state.container
    admin_token = _token(client)
    other = _activate_marker(client, admin_token, "second@example.edu")
    promoted = client.patch(f"/api/admin/users/{other['id']}", json={"role": "admin"}, headers=_headers(admin_token))
    assert promoted.status_code == 200

    me = client.get("/api/auth/me", headers=_headers(admin_token)).json()
    second_token = _token(client, "second@example.edu", GOOD_PASSWORD)
    deleted = client.delete(f"/api/admin/users/{me['userId']}", headers=_headers(second_token))
    assert deleted.status_code == 200

    outcome = asyncio.run(container.user_admin.ensure_bootstrap_admin())
    assert outcome.action == "existing"
    assert _login(client, "admin", "admin").status_code == 401


# --- invitation flow -------------------------------------------------------------


def test_invite_accept_and_sign_in_as_marker(tmp_path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)

    created = _invite(client, admin_token, "Marker@Example.EDU", displayName="  Dr  Marker ")
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["user"]["email"] == MARKER_EMAIL
    assert body["user"]["username"] == MARKER_EMAIL
    assert body["user"]["displayName"] == "Dr Marker"
    assert body["user"]["status"] == UserStatus.INVITED.value
    assert body["user"]["invite"]["expired"] is False
    assert body["mailSent"] is True
    # The recorder imitates a configured relay, so the link is not handed back.
    assert "inviteLink" not in body

    mailer = _mailer(client)
    assert mailer.last.to == MARKER_EMAIL
    assert "Dr Marker" in mailer.last.text
    link = mailer.last_link()
    assert link.startswith("http://localhost:5173/#/accept-invite/")
    token = mailer.last_token()

    # An invited account cannot sign in yet, and says so no differently.
    assert _login(client, MARKER_EMAIL, GOOD_PASSWORD).status_code == 401

    described = client.get(f"/api/auth/invitations/{token}")
    assert described.status_code == 200
    assert described.json()["valid"] is True
    assert described.json()["email"] == MARKER_EMAIL

    accepted = _accept(client, token, displayName="Dr M")
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"ok": True, "username": MARKER_EMAIL, "email": MARKER_EMAIL}

    marker = _login(client, MARKER_EMAIL, GOOD_PASSWORD)
    assert marker.status_code == 200
    assert marker.json()["role"] == UserRole.MARKER.value
    assert marker.json()["displayName"] == "Dr M"

    # Everything the app does is open to a marker ...
    marker_headers = _headers(marker.json()["token"])
    assert client.get("/api/sessions", headers=marker_headers).status_code == 200
    assert client.get("/api/settings", headers=marker_headers).status_code == 200
    # ... except account administration.
    assert client.get("/api/admin/users", headers=marker_headers).status_code == 403
    assert _invite(client, marker.json()["token"], "third@example.edu").status_code == 403


def test_invite_link_is_offered_to_the_admin_when_nothing_can_deliver_it(tmp_path) -> None:
    client = build_test_client(tmp_path)
    mailer = _mailer(client)
    mailer.configured = False  # the console backend's answer
    admin_token = _token(client)

    listing = client.get("/api/admin/users", headers=_headers(admin_token)).json()
    assert listing["mail"]["inviteLinkVisible"] is True

    created = _invite(client, admin_token)
    assert created.status_code == 201
    assert created.json()["mailSent"] is False
    assert created.json()["inviteLink"] == mailer.last_link()

    # The copied link works exactly like the emailed one.
    assert _accept(client, created.json()["inviteLink"].rsplit("/", 1)[-1]).status_code == 200


def test_a_failed_delivery_keeps_the_invited_account_and_reports_the_error(tmp_path) -> None:
    client = build_test_client(tmp_path)
    mailer = _mailer(client)
    mailer.fail_with = "relay refused: 550 no such mailbox"
    admin_token = _token(client)

    created = _invite(client, admin_token)
    assert created.status_code == 201
    assert created.json()["mailSent"] is False
    assert "550" in created.json()["mailError"]

    listing = client.get("/api/admin/users", headers=_headers(admin_token)).json()
    assert any(user["email"] == MARKER_EMAIL for user in listing["users"])

    mailer.fail_with = ""
    user_id = created.json()["user"]["id"]
    resent = client.post(f"/api/admin/users/{user_id}/resend-invite", headers=_headers(admin_token))
    assert resent.status_code == 200
    assert resent.json()["mailSent"] is True
    assert _accept(client, mailer.last_token()).status_code == 200


def test_inviting_an_existing_address_is_a_conflict(tmp_path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    assert _invite(client, admin_token).status_code == 201
    duplicate = _invite(client, admin_token, MARKER_EMAIL.upper())
    assert duplicate.status_code == 409
    assert MARKER_EMAIL in duplicate.json()["error"]


def test_invite_rejects_a_malformed_address_and_an_unknown_role(tmp_path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    assert _invite(client, admin_token, "not-an-address").status_code == 400
    assert _invite(client, admin_token, MARKER_EMAIL, role="superuser").status_code == 400


def test_resend_voids_the_earlier_link(tmp_path) -> None:
    client = build_test_client(tmp_path)
    mailer = _mailer(client)
    admin_token = _token(client)
    created = _invite(client, admin_token)
    first = mailer.last_token()
    user_id = created.json()["user"]["id"]

    assert client.post(f"/api/admin/users/{user_id}/resend-invite", headers=_headers(admin_token)).status_code == 200
    second = mailer.last_token()
    assert first != second

    assert client.get(f"/api/auth/invitations/{first}").json()["reason"] == "used"
    assert _accept(client, first).status_code == 410
    assert _accept(client, second).status_code == 200


def test_an_accepted_link_cannot_be_used_twice(tmp_path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    _invite(client, admin_token)
    token = _mailer(client).last_token()
    assert _accept(client, token).status_code == 200
    again = _accept(client, token, password="another-long-password")
    assert again.status_code == 410
    assert again.json()["error"] == "This link has already been used."
    # And the password set the first time is the one that works.
    assert _login(client, MARKER_EMAIL, GOOD_PASSWORD).status_code == 200
    assert _login(client, MARKER_EMAIL, "another-long-password").status_code == 401


def test_an_expired_link_is_refused_with_the_reason(tmp_path) -> None:
    client = build_test_client(tmp_path)
    container = client.app.state.container
    admin_token = _token(client)
    _invite(client, admin_token)
    token = _mailer(client).last_token()

    # Move the deployment's clock past the invitation's lifetime.
    container.user_admin._clock = lambda: utc_now() + timedelta(hours=container.settings.invite_token_ttl_hours + 1)

    described = client.get(f"/api/auth/invitations/{token}").json()
    assert described["valid"] is False
    assert described["reason"] == "expired"
    assert _accept(client, token).status_code == 410


def test_a_garbage_link_is_simply_invalid(tmp_path) -> None:
    client = build_test_client(tmp_path)
    described = client.get("/api/auth/invitations/not-a-real-token").json()
    assert described == {
        "valid": False,
        "reason": "invalid",
        "message": "This link is not valid.",
        "email": "",
        "displayName": "",
        "expiresAt": "",
    }
    assert _accept(client, "not-a-real-token").status_code == 410


def test_a_weak_password_does_not_burn_the_link(tmp_path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    _invite(client, admin_token)
    token = _mailer(client).last_token()

    short = _accept(client, token, password="short")
    assert short.status_code == 422
    assert "at least 10" in short.json()["error"]
    same_as_email = _accept(client, token, password=MARKER_EMAIL)
    assert same_as_email.status_code == 422
    assert "username or email" in same_as_email.json()["error"]

    # The link is still live: the first *acceptable* password activates it.
    assert client.get(f"/api/auth/invitations/{token}").json()["valid"] is True
    assert _accept(client, token).status_code == 200


def test_an_invitation_cannot_activate_an_account_that_was_disabled_meanwhile(tmp_path) -> None:
    """The link is judged against the account's state, not only its own expiry."""
    client = build_test_client(tmp_path)
    container = client.app.state.container
    admin_token = _token(client)
    created = _invite(client, admin_token)
    token = _mailer(client).last_token()
    user_id = created.json()["user"]["id"]

    # An invited account has no password, so the API refuses to "enable" it;
    # disable it directly to imitate an admin decision taken in the meantime.
    async def suspend() -> None:
        await container.users.update(user_id, lambda row: setattr(row, "status", UserStatus.DISABLED.value))
        container.user_directory.invalidate()

    asyncio.run(suspend())

    assert client.get(f"/api/auth/invitations/{token}").json()["reason"] == "account_unavailable"
    assert _accept(client, token).status_code == 410


# --- the account listing --------------------------------------------------------


def test_listing_never_carries_hashes_or_tokens(tmp_path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    _activate_marker(client, admin_token)
    _invite(client, admin_token, "pending@example.edu")

    listing = client.get("/api/admin/users", headers=_headers(admin_token))
    assert listing.status_code == 200
    body = listing.json()
    assert body["mail"] == {"backend": "recording", "configured": True, "inviteLinkVisible": False}
    serialised = json.dumps(body).lower()
    for forbidden in ("password", "hash", "tokenversion", "token_hash"):
        assert forbidden not in serialised, forbidden

    by_email = {user["email"]: user for user in body["users"]}
    assert by_email[MARKER_EMAIL]["status"] == UserStatus.ACTIVE.value
    assert by_email[MARKER_EMAIL]["invite"] is None
    assert by_email["pending@example.edu"]["status"] == UserStatus.INVITED.value
    assert by_email["pending@example.edu"]["invite"]["expired"] is False
    assert body["me"] == client.get("/api/auth/me", headers=_headers(admin_token)).json()["userId"]


# --- suspension, deletion, roles ---------------------------------------------------


def test_disabling_a_marker_ends_their_live_session_at_once(tmp_path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    marker = _activate_marker(client, admin_token)
    marker_headers = _headers(_token(client, MARKER_EMAIL, GOOD_PASSWORD))
    assert client.get("/api/sessions", headers=marker_headers).status_code == 200

    disabled = client.post(f"/api/admin/users/{marker['id']}/disable", headers=_headers(admin_token))
    assert disabled.status_code == 200
    assert disabled.json()["user"]["status"] == UserStatus.DISABLED.value

    # The very next request with the old token is refused, and so is a new login.
    assert client.get("/api/sessions", headers=marker_headers).status_code == 401
    assert _login(client, MARKER_EMAIL, GOOD_PASSWORD).status_code == 401

    enabled = client.post(f"/api/admin/users/{marker['id']}/enable", headers=_headers(admin_token))
    assert enabled.status_code == 200
    assert _login(client, MARKER_EMAIL, GOOD_PASSWORD).status_code == 200
    # But the token issued before the suspension stays dead.
    assert client.get("/api/sessions", headers=marker_headers).status_code == 401


def test_a_stream_ticket_dies_with_the_account(tmp_path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    marker = _activate_marker(client, admin_token)
    marker_token = _token(client, MARKER_EMAIL, GOOD_PASSWORD)
    ticket = client.get("/api/auth/stream-ticket", headers=_headers(marker_token)).json()["ticket"]

    scores_dir = client.app.state.container.settings.paths.output_scores_dir
    scores_dir.mkdir(parents=True, exist_ok=True)
    (scores_dir / "x.json").write_text("{}", encoding="utf-8")
    assert client.get(f"/media/scores/x.json?ticket={ticket}").status_code == 200

    client.post(f"/api/admin/users/{marker['id']}/disable", headers=_headers(admin_token))
    assert client.get(f"/media/scores/x.json?ticket={ticket}").status_code == 401


def test_deleting_a_marker_removes_them_and_their_sessions(tmp_path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    marker = _activate_marker(client, admin_token)
    marker_headers = _headers(_token(client, MARKER_EMAIL, GOOD_PASSWORD))

    assert client.delete(f"/api/admin/users/{marker['id']}", headers=_headers(admin_token)).status_code == 200
    assert client.get("/api/sessions", headers=marker_headers).status_code == 401
    assert _login(client, MARKER_EMAIL, GOOD_PASSWORD).status_code == 401
    assert client.delete(f"/api/admin/users/{marker['id']}", headers=_headers(admin_token)).status_code == 404
    # The address is free to be invited again.
    assert _invite(client, admin_token).status_code == 201


def test_a_role_change_applies_on_the_next_request_without_a_new_login(tmp_path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    marker = _activate_marker(client, admin_token)
    marker_headers = _headers(_token(client, MARKER_EMAIL, GOOD_PASSWORD))
    assert client.get("/api/admin/users", headers=marker_headers).status_code == 403

    promoted = client.patch(f"/api/admin/users/{marker['id']}", json={"role": "admin"}, headers=_headers(admin_token))
    assert promoted.status_code == 200
    assert promoted.json()["user"]["role"] == UserRole.ADMIN.value

    assert client.get("/api/admin/users", headers=marker_headers).status_code == 200
    assert client.get("/api/auth/me", headers=marker_headers).json()["role"] == UserRole.ADMIN.value

    demoted = client.patch(f"/api/admin/users/{marker['id']}", json={"role": "marker"}, headers=_headers(admin_token))
    assert demoted.status_code == 200
    assert client.get("/api/admin/users", headers=marker_headers).status_code == 403


def test_the_last_active_admin_cannot_be_demoted_disabled_or_deleted(tmp_path) -> None:
    """With one administrator, every way of removing their access is refused
    with "promote another account first" — including by themselves, where
    this guard deliberately answers before the self-guard would."""
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    me = client.get("/api/auth/me", headers=_headers(admin_token)).json()

    for attempt in (
        lambda: client.patch(f"/api/admin/users/{me['userId']}", json={"role": "marker"}, headers=_headers(admin_token)),
        lambda: client.post(f"/api/admin/users/{me['userId']}/disable", headers=_headers(admin_token)),
        lambda: client.delete(f"/api/admin/users/{me['userId']}", headers=_headers(admin_token)),
    ):
        response = attempt()
        assert response.status_code == 409, response.text
        assert "last active administrator" in response.json()["error"]

    # Promote a second admin and the same actions become the self-guard (400)
    # -- and the second admin may now act on the first.
    second = _activate_marker(client, admin_token, "second@example.edu")
    assert client.patch(f"/api/admin/users/{second['id']}", json={"role": "admin"}, headers=_headers(admin_token)).status_code == 200
    assert client.post(f"/api/admin/users/{me['userId']}/disable", headers=_headers(admin_token)).status_code == 400
    second_token = _token(client, "second@example.edu", GOOD_PASSWORD)
    assert client.patch(f"/api/admin/users/{me['userId']}", json={"role": "marker"}, headers=_headers(second_token)).status_code == 200

    # The second admin is the last one again: the guard is back.
    guard = client.post(f"/api/admin/users/{second['id']}/disable", headers=_headers(second_token))
    assert guard.status_code == 409
    # And the demoted first admin can no longer reach the screen at all.
    assert client.get("/api/admin/users", headers=_headers(admin_token)).status_code == 403


def test_nobody_changes_their_own_access(tmp_path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    me = client.get("/api/auth/me", headers=_headers(admin_token)).json()
    second = _activate_marker(client, admin_token, "second@example.edu")
    client.patch(f"/api/admin/users/{second['id']}", json={"role": "admin"}, headers=_headers(admin_token))

    # With another admin present the guard that answers is the self-guard.
    assert client.post(f"/api/admin/users/{me['userId']}/disable", headers=_headers(admin_token)).status_code == 400
    assert client.delete(f"/api/admin/users/{me['userId']}", headers=_headers(admin_token)).status_code == 400
    demote = client.patch(f"/api/admin/users/{me['userId']}", json={"role": "marker"}, headers=_headers(admin_token))
    assert demote.status_code == 400
    # A display-name edit on one's own row is fine.
    renamed = client.patch(f"/api/admin/users/{me['userId']}", json={"displayName": "Chief Examiner"}, headers=_headers(admin_token))
    assert renamed.status_code == 200
    assert renamed.json()["user"]["displayName"] == "Chief Examiner"


def test_an_invited_account_cannot_be_enabled_only_reinvited(tmp_path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    created = _invite(client, admin_token)
    user_id = created.json()["user"]["id"]
    enable = client.post(f"/api/admin/users/{user_id}/enable", headers=_headers(admin_token))
    assert enable.status_code == 409
    reset = client.post(f"/api/admin/users/{user_id}/send-password-reset", headers=_headers(admin_token))
    assert reset.status_code == 409


# --- password recovery ----------------------------------------------------------


def test_forgot_password_answers_identically_for_known_and_unknown_accounts(tmp_path) -> None:
    client = build_test_client(tmp_path)
    mailer = _mailer(client)
    admin_token = _token(client)
    _activate_marker(client, admin_token)
    sent_before = len(mailer.sent)

    unknown = client.post("/api/auth/password-reset/request", json={"identifier": "nobody@example.edu"})
    known = client.post("/api/auth/password-reset/request", json={"identifier": MARKER_EMAIL})
    assert unknown.status_code == known.status_code == 200
    assert unknown.json() == known.json()
    # Only the known address got a message.
    assert len(mailer.sent) == sent_before + 1
    assert mailer.last.to == MARKER_EMAIL
    assert mailer.last_link().startswith("http://localhost:5173/#/reset-password/")


def test_forgot_password_for_an_invited_account_resends_the_invitation(tmp_path) -> None:
    client = build_test_client(tmp_path)
    mailer = _mailer(client)
    admin_token = _token(client)
    _invite(client, admin_token)
    first = mailer.last_token()

    assert client.post("/api/auth/password-reset/request", json={"identifier": MARKER_EMAIL}).status_code == 200
    assert "accept-invite" in mailer.last_link()
    assert mailer.last_token() != first
    assert _accept(client, mailer.last_token()).status_code == 200


def test_reset_link_sets_a_new_password_ends_other_sessions_and_notifies(tmp_path) -> None:
    client = build_test_client(tmp_path)
    mailer = _mailer(client)
    admin_token = _token(client)
    _activate_marker(client, admin_token)
    old_session = _headers(_token(client, MARKER_EMAIL, GOOD_PASSWORD))

    client.post("/api/auth/password-reset/request", json={"identifier": MARKER_EMAIL})
    token = mailer.last_token()
    described = client.get(f"/api/auth/password-reset/{token}").json()
    assert described["valid"] is True and described["email"] == MARKER_EMAIL

    confirmed = client.post(f"/api/auth/password-reset/{token}/confirm", json={"password": "brand-new-password-1"})
    assert confirmed.status_code == 200, confirmed.text

    assert client.get("/api/sessions", headers=old_session).status_code == 401
    assert _login(client, MARKER_EMAIL, GOOD_PASSWORD).status_code == 401
    assert _login(client, MARKER_EMAIL, "brand-new-password-1").status_code == 200
    assert "password was changed" in mailer.last.subject
    # Single use.
    assert client.post(f"/api/auth/password-reset/{token}/confirm", json={"password": "yet-another-password"}).status_code == 410


def test_admin_can_send_a_reset_on_the_users_behalf(tmp_path) -> None:
    client = build_test_client(tmp_path)
    mailer = _mailer(client)
    admin_token = _token(client)
    marker = _activate_marker(client, admin_token)

    sent = client.post(f"/api/admin/users/{marker['id']}/send-password-reset", headers=_headers(admin_token))
    assert sent.status_code == 200
    assert sent.json()["mailSent"] is True
    assert "resetLink" not in sent.json()
    assert mailer.last.to == MARKER_EMAIL

    mailer.configured = False
    sent = client.post(f"/api/admin/users/{marker['id']}/send-password-reset", headers=_headers(admin_token))
    assert sent.json()["resetLink"] == mailer.last_link()


def test_change_own_password_keeps_this_session_and_ends_the_others(tmp_path) -> None:
    client = build_test_client(tmp_path)
    mailer = _mailer(client)
    admin_token = _token(client)
    _activate_marker(client, admin_token)
    this_tab = _token(client, MARKER_EMAIL, GOOD_PASSWORD)
    other_tab = _token(client, MARKER_EMAIL, GOOD_PASSWORD)

    wrong = client.post(
        "/api/auth/password",
        json={"currentPassword": "not-it", "newPassword": "brand-new-password-1"},
        headers=_headers(this_tab),
    )
    assert wrong.status_code == 400
    weak = client.post(
        "/api/auth/password",
        json={"currentPassword": GOOD_PASSWORD, "newPassword": "short"},
        headers=_headers(this_tab),
    )
    assert weak.status_code == 422

    changed = client.post(
        "/api/auth/password",
        json={"currentPassword": GOOD_PASSWORD, "newPassword": "brand-new-password-1"},
        headers=_headers(this_tab),
    )
    assert changed.status_code == 200, changed.text
    fresh = changed.json()["token"]
    assert fresh != this_tab

    assert client.get("/api/sessions", headers=_headers(fresh)).status_code == 200
    assert client.get("/api/sessions", headers=_headers(this_tab)).status_code == 401
    assert client.get("/api/sessions", headers=_headers(other_tab)).status_code == 401
    assert _login(client, MARKER_EMAIL, "brand-new-password-1").status_code == 200
    assert mailer.last.to == MARKER_EMAIL and "changed" in mailer.last.subject


# --- throttling and the open paths ----------------------------------------------


def test_public_token_endpoints_are_rate_limited_per_ip(tmp_path) -> None:
    client = build_test_client(tmp_path)
    limit = client.app.state.container.settings.token_rate_limit_max_attempts
    for _ in range(limit):
        assert client.get("/api/auth/invitations/nope").status_code == 200
    throttled = client.get("/api/auth/invitations/nope")
    assert throttled.status_code == 429


def test_public_token_endpoints_need_no_session_but_everything_else_still_does(tmp_path) -> None:
    client = build_test_client(tmp_path)
    assert client.get("/api/auth/invitations/x").status_code == 200
    assert client.get("/api/auth/password-reset/x").status_code == 200
    assert client.post("/api/auth/password-reset/request", json={"identifier": "x"}).status_code == 200
    assert client.get("/api/admin/users").status_code == 401
    assert client.post("/api/auth/password", json={"currentPassword": "a", "newPassword": "b" * 12}).status_code == 401


def test_tokens_issued_before_accounts_existed_are_refused(tmp_path) -> None:
    """A token with no ``sub`` predates accounts-as-rows: sign in again."""
    from app.core.security import sign_payload

    client = build_test_client(tmp_path)
    container = client.app.state.container
    legacy = sign_payload(
        {"username": "admin", "issuedAt": 1, "expiresAt": 4102444800000, "tokenId": "legacy"},
        container.auth.runtime.auth_secret,
    )
    assert client.get("/api/sessions", headers=_headers(legacy)).status_code == 401


def test_repository_consumes_a_token_exactly_once_under_a_race(tmp_path) -> None:
    """The conditional update is what makes a link single-use: of N racing
    consumers exactly one wins, without a lock table."""
    client = build_test_client(tmp_path)
    container = client.app.state.container
    admin_token = _token(client)
    _invite(client, admin_token)
    from app.core.security import hash_action_token

    token_hash = hash_action_token(_mailer(client).last_token())

    async def race() -> list[object]:
        return await asyncio.gather(
            *(container.users.consume_token(token_hash, ActionTokenPurpose.INVITE) for _ in range(5))
        )

    results = asyncio.run(race())
    assert sum(1 for result in results if result is not None) == 1
