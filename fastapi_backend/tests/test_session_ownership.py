"""A session, and the job/upload rows a run of it produces, may be changed
only by its creator or an admin. Every marker still SEES every session — see
test_session_provenance.py for the read side, which is untouched — this file
is the write side: app.domain.access + the require_*_owner dependencies
(app/api/dependencies.py) applied to the mutating routes.
"""

from __future__ import annotations

import asyncio

from tests.test_routes import build_test_client
from tests.test_user_admin import GOOD_PASSWORD, MARKER_EMAIL, _activate_marker, _headers, _token

UPLOAD = {
    "workflow": "standard",
    "autoProcess": False,
    "files": [
        {"kind": "video", "originalName": "station.mp4", "mimeType": "video/mp4", "sizeBytes": 5},
        {"kind": "caseStudy", "originalName": "case.pdf", "mimeType": "application/pdf", "sizeBytes": 4},
    ],
}

SECOND_EMAIL = "second@example.edu"


def _second_marker_token(client, admin_token: str) -> str:
    _activate_marker(client, admin_token, SECOND_EMAIL)
    return _token(client, SECOND_EMAIL, GOOD_PASSWORD)


def _owned_session(client, owner_token: str) -> str:
    created = client.post("/api/uploads/initiate", headers=_headers(owner_token), json=UPLOAD)
    assert created.status_code == 201, created.text
    return created.json()["session"]["id"]


# --- sessions -----------------------------------------------------------------------


def test_a_marker_cannot_rename_another_markers_session(tmp_path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    _activate_marker(client, admin_token, MARKER_EMAIL)
    owner_token = _token(client, MARKER_EMAIL, GOOD_PASSWORD)
    other_token = _second_marker_token(client, admin_token)
    session_id = _owned_session(client, owner_token)

    forbidden = client.patch(
        f"/api/sessions/{session_id}/name", json={"name": "Hijacked"}, headers=_headers(other_token)
    )
    assert forbidden.status_code == 403, forbidden.text

    allowed = client.patch(
        f"/api/sessions/{session_id}/name", json={"name": "Round 2"}, headers=_headers(owner_token)
    )
    assert allowed.status_code == 200, allowed.text


def test_a_marker_cannot_delete_another_markers_session(tmp_path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    _activate_marker(client, admin_token, MARKER_EMAIL)
    owner_token = _token(client, MARKER_EMAIL, GOOD_PASSWORD)
    other_token = _second_marker_token(client, admin_token)
    session_id = _owned_session(client, owner_token)

    forbidden = client.delete(f"/api/sessions/{session_id}", headers=_headers(other_token))
    assert forbidden.status_code == 403, forbidden.text

    # The owner (not just an admin) may still delete their own session.
    allowed = client.delete(f"/api/sessions/{session_id}", headers=_headers(owner_token))
    assert allowed.status_code == 200, allowed.text


def test_an_admin_may_mutate_any_session(tmp_path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    _activate_marker(client, admin_token, MARKER_EMAIL)
    owner_token = _token(client, MARKER_EMAIL, GOOD_PASSWORD)
    session_id = _owned_session(client, owner_token)

    renamed = client.patch(
        f"/api/sessions/{session_id}/name", json={"name": "Admin renamed"}, headers=_headers(admin_token)
    )
    assert renamed.status_code == 200, renamed.text
    deleted = client.delete(f"/api/sessions/{session_id}", headers=_headers(admin_token))
    assert deleted.status_code == 200, deleted.text


def test_every_marker_may_still_read_another_markers_session(tmp_path) -> None:
    """The gate is on writes only — GET is untouched."""
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    _activate_marker(client, admin_token, MARKER_EMAIL)
    owner_token = _token(client, MARKER_EMAIL, GOOD_PASSWORD)
    other_token = _second_marker_token(client, admin_token)
    session_id = _owned_session(client, owner_token)

    listing = client.get("/api/sessions", headers=_headers(other_token))
    assert listing.status_code == 200
    detail = client.get(f"/api/sessions/{session_id}", headers=_headers(other_token))
    assert detail.status_code == 200


def test_a_session_with_no_recorded_creator_is_mutable_by_any_marker(tmp_path) -> None:
    """A row written before accounts existed has no owner to defer to."""
    client = build_test_client(tmp_path)
    container = client.app.state.container
    admin_token = _token(client)
    _activate_marker(client, admin_token, MARKER_EMAIL)
    marker_token = _token(client, MARKER_EMAIL, GOOD_PASSWORD)

    async def write_legacy() -> None:
        await container.sessions.repository.write(
            {"id": "legacy-1", "name": "Old", "status": "completed", "createdAt": "2026-01-01T00:00:00Z", "outputs": {}}
        )

    asyncio.run(write_legacy())
    renamed = client.patch(
        "/api/sessions/legacy-1/name", json={"name": "Still open"}, headers=_headers(marker_token)
    )
    assert renamed.status_code == 200, renamed.text


def test_mutating_a_missing_session_is_404_not_403(tmp_path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    _activate_marker(client, admin_token, MARKER_EMAIL)
    marker_token = _token(client, MARKER_EMAIL, GOOD_PASSWORD)

    response = client.delete("/api/sessions/does-not-exist", headers=_headers(marker_token))
    assert response.status_code == 404, response.text


# --- jobs ---------------------------------------------------------------------------


def test_a_marker_cannot_cancel_another_markers_job(tmp_path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    _activate_marker(client, admin_token, MARKER_EMAIL)
    owner_token = _token(client, MARKER_EMAIL, GOOD_PASSWORD)
    other_token = _second_marker_token(client, admin_token)

    created = client.post("/api/uploads/initiate", headers=_headers(owner_token), json=UPLOAD)
    job_id = created.json()["job"]["id"]

    forbidden = client.post(f"/api/jobs/{job_id}/cancel", headers=_headers(other_token))
    assert forbidden.status_code == 403, forbidden.text

    allowed = client.post(f"/api/jobs/{job_id}/cancel", headers=_headers(owner_token))
    assert allowed.status_code != 403, allowed.text


# --- uploads ------------------------------------------------------------------------


def test_a_marker_cannot_upload_a_part_onto_another_markers_upload(tmp_path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    _activate_marker(client, admin_token, MARKER_EMAIL)
    owner_token = _token(client, MARKER_EMAIL, GOOD_PASSWORD)
    other_token = _second_marker_token(client, admin_token)

    created = client.post("/api/uploads/initiate", headers=_headers(owner_token), json=UPLOAD)
    upload_id = created.json()["uploadId"]
    video_file_id = created.json()["fileUploads"][0]["fileId"]

    forbidden = client.put(
        f"/api/uploads/{upload_id}/parts/1",
        params={"fileId": video_file_id},
        headers=_headers(other_token),
        content=b"12345",
    )
    assert forbidden.status_code == 403, forbidden.text

    allowed = client.put(
        f"/api/uploads/{upload_id}/parts/1",
        params={"fileId": video_file_id},
        headers=_headers(owner_token),
        content=b"12345",
    )
    assert allowed.status_code == 200, allowed.text
