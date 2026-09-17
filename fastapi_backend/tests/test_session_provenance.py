"""Every session records who created it.

The uploader on a recording, and whoever queued a clip's assessment on the
child — as a snapshot of the account at that moment, mirrored into an indexed
column, carried by the list projection, and untouched by anything that
happens to the account or the session afterwards.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.database.models import SessionRecord
from app.domain.actors import PROVENANCE_KEY, Actor, provenance_of, provenance_user_id
from tests.test_clip_assess_idempotent import _build as build_clip_service, _children
from tests.test_routes import build_test_client
from tests.test_user_admin import GOOD_PASSWORD, MARKER_EMAIL, _activate_marker, _headers, _token


UPLOAD = {
    "workflow": "standard",
    "autoProcess": True,
    "files": [
        {"kind": "video", "originalName": "station.mp4", "mimeType": "video/mp4", "sizeBytes": 5},
        {"kind": "caseStudy", "originalName": "case.pdf", "mimeType": "application/pdf", "sizeBytes": 4},
    ],
}


# --- the value type ---------------------------------------------------------------


def test_actor_is_read_from_a_verified_payload_and_nothing_else() -> None:
    actor = Actor.from_auth_payload({"sub": "u-1", "username": "m@x.edu", "displayName": " Dr M ", "role": "marker"})
    assert actor == Actor(user_id="u-1", username="m@x.edu", display_name="Dr M")
    assert actor.label == "Dr M"
    assert Actor(user_id="u-2", username="admin").label == "admin"
    assert actor.to_provenance() == {"userId": "u-1", "username": "m@x.edu", "displayName": "Dr M"}
    # No session, or a token from before accounts existed: no actor.
    assert Actor.from_auth_payload(None) is None
    assert Actor.from_auth_payload({"username": "admin"}) is None
    assert Actor.from_auth_payload({"sub": "u-1"}) is None


def test_provenance_is_read_tolerantly() -> None:
    assert provenance_of({PROVENANCE_KEY: {"userId": "u-1", "username": "a", "displayName": "A"}}) == {
        "userId": "u-1",
        "username": "a",
        "displayName": "A",
    }
    assert provenance_of({PROVENANCE_KEY: {"username": "a"}}) == {"userId": "", "username": "a", "displayName": ""}
    for absent in ({}, {PROVENANCE_KEY: None}, {PROVENANCE_KEY: "u-1"}, {PROVENANCE_KEY: {}}, None, "x"):
        assert provenance_of(absent) is None, absent
    assert provenance_user_id({PROVENANCE_KEY: {"userId": "u-1", "username": "a"}}) == "u-1"
    assert provenance_user_id({PROVENANCE_KEY: {"username": "a"}}) is None


# --- uploads -------------------------------------------------------------------------


def test_an_upload_records_the_signed_in_account_as_its_creator(tmp_path) -> None:
    client = build_test_client(tmp_path)
    container = client.app.state.container
    admin_token = _token(client)
    _activate_marker(client, admin_token)
    marker_token = _token(client, MARKER_EMAIL, GOOD_PASSWORD)
    me = client.get("/api/auth/me", headers=_headers(marker_token)).json()

    created = client.post("/api/uploads/initiate", headers=_headers(marker_token), json=UPLOAD)
    assert created.status_code == 201, created.text
    session = created.json()["session"]
    assert session["createdBy"] == {"userId": me["userId"], "username": MARKER_EMAIL, "displayName": ""}

    # The full document, the list projection, and the indexed column all agree.
    detail = client.get(f"/api/sessions/{session['id']}", headers=_headers(admin_token)).json()
    assert detail["session"]["createdBy"]["userId"] == me["userId"]

    listing = client.get("/api/sessions", headers=_headers(admin_token)).json()
    [entry] = [item for item in listing["sessions"] if item["id"] == session["id"]]
    assert entry["createdBy"] == session["createdBy"]

    async def column() -> str | None:
        async with container.orm_database.session() as db:
            return await db.scalar(select(SessionRecord.created_by).where(SessionRecord.id == session["id"]))

    assert asyncio.run(column()) == me["userId"]


def test_the_creator_is_a_snapshot_that_outlives_the_account(tmp_path) -> None:
    """Renaming, disabling or deleting the uploader must not rewrite who
    uploaded what — the record says who it was *then*."""
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    marker = _activate_marker(client, admin_token)
    marker_token = _token(client, MARKER_EMAIL, GOOD_PASSWORD)
    session_id = client.post("/api/uploads/initiate", headers=_headers(marker_token), json=UPLOAD).json()["session"]["id"]

    client.patch(f"/api/admin/users/{marker['id']}", json={"displayName": "Renamed"}, headers=_headers(admin_token))
    client.delete(f"/api/admin/users/{marker['id']}", headers=_headers(admin_token))

    detail = client.get(f"/api/sessions/{session_id}", headers=_headers(admin_token)).json()["session"]
    assert detail["createdBy"] == {"userId": marker["id"], "username": MARKER_EMAIL, "displayName": ""}


def test_renaming_a_session_keeps_its_creator(tmp_path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    me = client.get("/api/auth/me", headers=_headers(admin_token)).json()
    session_id = client.post("/api/uploads/initiate", headers=_headers(admin_token), json=UPLOAD).json()["session"]["id"]

    renamed = client.patch(f"/api/sessions/{session_id}/name", json={"name": "Round 2"}, headers=_headers(admin_token))
    assert renamed.status_code == 200, renamed.text
    detail = client.get(f"/api/sessions/{session_id}", headers=_headers(admin_token)).json()["session"]
    assert detail["name"] == "Round 2"
    assert detail["createdBy"]["userId"] == me["userId"]


def test_a_session_from_before_creators_were_recorded_has_none(tmp_path) -> None:
    client = build_test_client(tmp_path)
    container = client.app.state.container
    admin_token = _token(client)

    async def write_legacy() -> None:
        await container.sessions.repository.write(
            {"id": "legacy-1", "name": "Old", "status": "completed", "createdAt": "2026-01-01T00:00:00Z", "outputs": {}}
        )

    asyncio.run(write_legacy())
    listing = client.get("/api/sessions", headers=_headers(admin_token)).json()
    [entry] = [item for item in listing["sessions"] if item["id"] == "legacy-1"]
    assert entry["createdBy"] is None


# --- clip children -------------------------------------------------------------------


def test_a_clip_child_records_who_queued_its_assessment(tmp_path) -> None:
    service, store, _jobs, _maintenance, _tmp = build_clip_service(tmp_path)
    actor = Actor(user_id="u-9", username="m@x.edu", display_name="Dr M")

    asyncio.run(service.assess_clip("parent-1", "clip-a", actor=actor))

    [child] = _children(store)
    assert child["createdBy"] == {"userId": "u-9", "username": "m@x.edu", "displayName": "Dr M"}


def test_a_clip_child_inherits_the_recordings_uploader_when_no_actor_is_given(tmp_path) -> None:
    service, store, _jobs, _maintenance, _tmp = build_clip_service(tmp_path)
    store.sessions["parent-1"][PROVENANCE_KEY] = {"userId": "u-1", "username": "admin", "displayName": ""}

    asyncio.run(service.assess_clip("parent-1", "clip-a"))

    [child] = _children(store)
    assert child["createdBy"] == {"userId": "u-1", "username": "admin", "displayName": ""}
