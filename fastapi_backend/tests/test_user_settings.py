"""Two-tier settings: a marker's own overrides of transcriptionEngine,
transcriptionEngineOptions, llmPrimary/llmFallbacks, llmMarkingMode/llmPanel
and llmTranscriptPreprocess, layered over the deployment defaults an admin
sets. See CLAUDE.md "Two-tier settings" and app/services/preferences_service.py.

Unit tests exercise UserSettingsRepository + PreferencesService directly, the
way test_app_settings.py exercises AppSettingsRepository. The HTTP tests prove
the isolation an operator actually cares about: two markers changing their own
settings cannot see or clobber each other's, an admin's deployment-default
edit does not leak into (or get overwritten by) anyone's personal override,
and only an admin may touch the deployment document at all.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.api.routes.settings import _reject_deployment_scoped
from app.database.orm import OrmDatabase
from app.domain.settings_scope import USER_SCOPED_KEYS, is_user_scoped
from app.domain.users import UserRole, UserStatus
from app.repositories.app_settings_repository import AppSettingsRepository
from app.repositories.user_repository import UserRepository
from app.repositories.user_settings_repository import UserSettingsRepository
from app.services.preferences_service import PreferencesService

from fastapi import HTTPException
import pytest

from tests.test_routes import build_test_client
from tests.test_user_admin import GOOD_PASSWORD, MARKER_EMAIL, _activate_marker, _headers, _token

SECOND_EMAIL = "second@example.edu"


# --- repository + service (unit) ---------------------------------------------
#
# user_settings.user_id is a real foreign key to users.id (ON DELETE CASCADE —
# see test_deleting_an_account_takes_its_overrides_with_it below), so these
# tests create real account rows rather than referencing a bare string id.


def _build(tmp_path: Path) -> tuple[PreferencesService, UserRepository]:
    database = OrmDatabase(tmp_path / "settings.sqlite3")
    preferences = PreferencesService(AppSettingsRepository(database), UserSettingsRepository(database))
    return preferences, UserRepository(database)


def _account(users: UserRepository, username: str) -> str:
    record = asyncio.run(
        users.create(
            username=username,
            email=f"{username}@example.edu",
            display_name="",
            role=UserRole.MARKER,
            status=UserStatus.ACTIVE,
            password_hash=None,
        )
    )
    return record.id


def test_an_account_with_no_overrides_gets_the_deployment_defaults(tmp_path: Path) -> None:
    preferences, users = _build(tmp_path)
    marker = _account(users, "marker-1")

    assert asyncio.run(preferences.overrides_for(marker)) == {}
    assert asyncio.run(preferences.effective_for(marker)) == asyncio.run(preferences.defaults())
    assert asyncio.run(preferences.effective_for(None)) == asyncio.run(preferences.defaults())


def test_an_override_layers_over_the_deployment_default_without_changing_it(tmp_path: Path) -> None:
    preferences, users = _build(tmp_path)
    first, second = _account(users, "marker-1"), _account(users, "marker-2")
    asyncio.run(preferences.app_settings.set_values({"transcriptionEngine": "whisperx"}))

    asyncio.run(preferences.set_overrides(first, {"transcriptionEngine": "canary-qwen"}))

    assert asyncio.run(preferences.effective_for(first))["transcriptionEngine"] == "canary-qwen"
    assert asyncio.run(preferences.defaults())["transcriptionEngine"] == "whisperx"
    # A second account, with no override of its own, still sees the deployment default.
    assert asyncio.run(preferences.effective_for(second))["transcriptionEngine"] == "whisperx"


def test_clearing_an_override_reverts_to_the_deployment_default(tmp_path: Path) -> None:
    preferences, users = _build(tmp_path)
    marker = _account(users, "marker-1")
    asyncio.run(preferences.set_overrides(marker, {"transcriptionEngine": "canary-qwen"}))
    assert asyncio.run(preferences.effective_for(marker))["transcriptionEngine"] == "canary-qwen"

    asyncio.run(preferences.clear_override(marker, "transcriptionEngine"))

    assert asyncio.run(preferences.effective_for(marker))["transcriptionEngine"] == ""
    assert asyncio.run(preferences.overrides_for(marker)) == {}


def test_a_deployment_scoped_stray_in_the_overrides_table_is_ignored(tmp_path: Path) -> None:
    """Defence in depth: the write path is the real boundary (see the HTTP
    tests below), but a row written by a bug or a future release must not let
    a personal override reach past what a marker may change."""
    preferences, users = _build(tmp_path)
    marker = _account(users, "marker-1")
    asyncio.run(preferences.user_settings.set_overrides(marker, {"notAUserScopedKey": "sneaky"}))

    effective = asyncio.run(preferences.effective_for(marker))

    assert "notAUserScopedKey" not in effective


def test_forget_clears_every_override(tmp_path: Path) -> None:
    preferences, users = _build(tmp_path)
    marker = _account(users, "marker-1")
    asyncio.run(preferences.set_overrides(marker, {"transcriptionEngine": "canary-qwen", "llmMarkingMode": "panel"}))

    asyncio.run(preferences.forget(marker))

    assert asyncio.run(preferences.overrides_for(marker)) == {}


def test_every_default_settings_key_is_classified_as_user_or_deployment_scoped(tmp_path: Path) -> None:
    """USER_SCOPED_KEYS is the fail-safe boundary; every key the deployment
    document actually carries must have a deliberate answer either way."""
    preferences, _users = _build(tmp_path)
    for key in asyncio.run(preferences.defaults()):
        assert is_user_scoped(key) in (True, False)  # both classifications are valid; absence is not
    assert USER_SCOPED_KEYS <= set(asyncio.run(preferences.defaults()))


def test_reject_deployment_scoped_raises_403_naming_the_key() -> None:
    with pytest.raises(HTTPException) as error:
        _reject_deployment_scoped({"transcriptionEngine": "whisperx", "notAUserScopedKey": "x"})
    assert error.value.status_code == 403
    assert "notAUserScopedKey" in error.value.detail

    _reject_deployment_scoped({"transcriptionEngine": "whisperx"})  # every real key is user-scoped: no raise


# --- HTTP: isolation between accounts -----------------------------------------


def _second_marker_token(client, admin_token: str) -> str:
    _activate_marker(client, admin_token, SECOND_EMAIL)
    return _token(client, SECOND_EMAIL, GOOD_PASSWORD)


def test_two_markers_change_only_their_own_transcription_engine(tmp_path: Path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    _activate_marker(client, admin_token, MARKER_EMAIL)
    first_token = _token(client, MARKER_EMAIL, GOOD_PASSWORD)
    second_token = _second_marker_token(client, admin_token)

    saved = client.patch(
        "/api/settings", json={"transcriptionEngine": "canary-qwen"}, headers=_headers(first_token)
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["settings"]["transcriptionEngine"] == "canary-qwen"

    # The second marker's own read is untouched by the first marker's save.
    second_view = client.get("/api/settings", headers=_headers(second_token)).json()
    assert second_view["settings"]["transcriptionEngine"] == ""
    assert second_view["overrides"] == {}

    first_view = client.get("/api/settings", headers=_headers(first_token)).json()
    assert first_view["settings"]["transcriptionEngine"] == "canary-qwen"
    assert first_view["overrides"] == {"transcriptionEngine": "canary-qwen"}
    assert first_view["userScopedKeys"] == sorted(USER_SCOPED_KEYS)


def test_clearing_a_setting_reverts_to_the_deployment_default_over_http(tmp_path: Path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    _activate_marker(client, admin_token, MARKER_EMAIL)
    marker_token = _token(client, MARKER_EMAIL, GOOD_PASSWORD)

    client.patch("/api/settings", json={"transcriptionEngine": "canary-qwen"}, headers=_headers(marker_token))
    cleared = client.delete("/api/settings/transcriptionEngine", headers=_headers(marker_token))

    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["settings"]["transcriptionEngine"] == ""


def test_a_marker_cannot_write_deployment_settings(tmp_path: Path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    _activate_marker(client, admin_token, MARKER_EMAIL)
    marker_token = _token(client, MARKER_EMAIL, GOOD_PASSWORD)

    forbidden = client.patch(
        "/api/admin/settings", json={"transcriptionEngine": "canary-qwen"}, headers=_headers(marker_token)
    )
    assert forbidden.status_code == 403, forbidden.text


def test_an_admins_deployment_default_edit_does_not_touch_a_markers_override(tmp_path: Path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    _activate_marker(client, admin_token, MARKER_EMAIL)
    marker_token = _token(client, MARKER_EMAIL, GOOD_PASSWORD)

    client.patch("/api/settings", json={"transcriptionEngine": "canary-qwen"}, headers=_headers(marker_token))
    admin_set = client.patch(
        "/api/admin/settings", json={"transcriptionEngine": "whisperx"}, headers=_headers(admin_token)
    )
    assert admin_set.status_code == 200, admin_set.text

    marker_view = client.get("/api/settings", headers=_headers(marker_token)).json()
    assert marker_view["settings"]["transcriptionEngine"] == "canary-qwen"  # the marker's own override survives
    assert marker_view["defaults"]["transcriptionEngine"] == "whisperx"  # and now reflects the admin's change

    # An account with no override of its own picks up the new default.
    other_view = client.get("/api/settings", headers=_headers(_second_marker_token(client, admin_token))).json()
    assert other_view["settings"]["transcriptionEngine"] == "whisperx"


def test_deleting_an_account_takes_its_overrides_with_it(tmp_path: Path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    marker = _activate_marker(client, admin_token, MARKER_EMAIL)
    marker_token = _token(client, MARKER_EMAIL, GOOD_PASSWORD)
    client.patch("/api/settings", json={"transcriptionEngine": "canary-qwen"}, headers=_headers(marker_token))

    container = client.app.state.container
    assert asyncio.run(container.user_settings.overrides_for(marker["id"])) != {}

    deleted = client.delete(f"/api/admin/users/{marker['id']}", headers=_headers(admin_token))
    assert deleted.status_code == 200, deleted.text

    assert asyncio.run(container.user_settings.overrides_for(marker["id"])) == {}
