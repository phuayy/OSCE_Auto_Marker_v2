"""Batch upload: one charge against the hourly budget buys N initiates.

Two layers. ``UploadBatchAllowances`` is the in-process store (bounds, owner
scoping, expiry, the open-batch cap, concurrency); the HTTP tests pin how the
routes and ``require_upload_admission`` use it — most importantly that a batch
is never answered by quietly charging the hourly budget instead.
"""

from __future__ import annotations

import threading

import pytest

from app.core.exceptions import AppError
from app.services.async_upload_service import VIDEO_EXTENSIONS
from app.services.upload_batches import UploadBatchAllowances
from tests.test_routes import build_test_client
from tests.test_session_ownership import _second_marker_token
from tests.test_user_admin import _headers, _token

UPLOAD = {
    "workflow": "standard",
    "autoProcess": False,
    "files": [
        {"kind": "video", "originalName": "station.mp4", "mimeType": "video/mp4", "sizeBytes": 5},
        {"kind": "caseStudy", "originalName": "case.pdf", "mimeType": "application/pdf", "sizeBytes": 4},
    ],
}


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def allowances(clock: Clock | None = None, **overrides: int) -> UploadBatchAllowances:
    settings = {"max_size": 50, "ttl_seconds": 100, "max_open_per_user": 2, **overrides}
    return UploadBatchAllowances(clock=clock or Clock(), **settings)


# --- the store ------------------------------------------------------------------


@pytest.mark.parametrize("count", [-1, 0, 1, 51])
def test_count_outside_the_bounds_is_refused(count):
    with pytest.raises(AppError) as refused:
        allowances().open("a", count)
    assert refused.value.status_code == 422


@pytest.mark.parametrize("count", [2, 50])
def test_count_at_the_bounds_is_accepted(count):
    batch = allowances().open("a", count)
    assert (batch.count, batch.remaining) == (count, count)
    assert len(batch.batch_id) == 32 and batch.expires_at.endswith("Z")


def test_consume_spends_one_unit_until_the_batch_is_used_up():
    store = allowances()
    batch = store.open("a", 2)
    store.consume("a", batch.batch_id)
    store.consume("a", batch.batch_id)
    with pytest.raises(AppError) as exhausted:
        store.consume("a", batch.batch_id)
    assert exhausted.value.status_code == 409 and "no uploads left" in exhausted.value.message


def test_another_accounts_batch_id_is_unknown():
    store = allowances()
    batch = store.open("a", 3)
    with pytest.raises(AppError) as refused:
        store.consume("b", batch.batch_id)
    assert refused.value.status_code == 409
    store.consume("a", batch.batch_id)


@pytest.mark.parametrize("batch_id", ["", "nope", "x" * 5000])
def test_unknown_ids_are_refused(batch_id):
    store = allowances()
    store.open("a", 3)
    with pytest.raises(AppError) as refused:
        store.consume("a", batch_id)
    assert refused.value.status_code == 409


def test_an_expired_batch_is_refused_and_forgotten():
    clock = Clock()
    store = allowances(clock, ttl_seconds=100)
    batch = store.open("a", 3)
    clock.now += 100
    with pytest.raises(AppError) as refused:
        store.consume("a", batch.batch_id)
    assert "expired" in refused.value.message
    store.release("a", batch.batch_id)  # nothing to refund, and no error


def test_open_batches_are_capped_per_account_but_not_across_accounts():
    store = allowances(max_open_per_user=2)
    store.open("a", 3)
    store.open("a", 3)
    with pytest.raises(AppError) as capped:
        store.open("a", 3)
    assert capped.value.status_code == 429
    store.open("b", 3)


def test_expiry_and_use_free_a_slot_in_the_cap():
    clock = Clock()
    store = allowances(clock, max_open_per_user=1)
    first = store.open("a", 2)
    store.consume("a", first.batch_id)
    store.consume("a", first.batch_id)
    second = store.open("a", 2)  # the spent batch no longer counts
    clock.now += 101
    store.open("a", 2)  # the expired one no longer counts either
    with pytest.raises(AppError):
        store.consume("a", second.batch_id)


def test_memory_is_pruned_of_expired_batches_of_accounts_that_never_return():
    clock = Clock()
    store = allowances(clock, ttl_seconds=10)
    for user in range(200):
        store.open(f"user-{user}", 2)
    clock.now += 11
    store.open("fresh", 2)
    assert list(store._by_user) == ["fresh"]


def test_release_gives_a_unit_back_but_never_beyond_the_count():
    store = allowances()
    batch = store.open("a", 2)
    store.consume("a", batch.batch_id)
    store.release("a", batch.batch_id)
    store.release("a", batch.batch_id)
    store.consume("a", batch.batch_id)
    store.consume("a", batch.batch_id)
    with pytest.raises(AppError):
        store.consume("a", batch.batch_id)


def test_concurrent_consumes_never_over_consume():
    store = allowances()
    batch = store.open("a", 10)
    outcomes: list[bool] = []
    barrier = threading.Barrier(40)

    def spend() -> None:
        barrier.wait()
        try:
            store.consume("a", batch.batch_id)
            outcomes.append(True)
        except AppError:
            outcomes.append(False)

    threads = [threading.Thread(target=spend) for _ in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count(True) == 10 and outcomes.count(False) == 30


# --- the routes -----------------------------------------------------------------


def _open(client, token, count=3):
    return client.post("/api/uploads/batches", headers=_headers(token), json={"count": count})


def _initiate(client, token, batch_id=None):
    headers = _headers(token)
    if batch_id is not None:
        headers["X-Upload-Batch"] = batch_id
    return client.post("/api/uploads/initiate", headers=headers, json=UPLOAD)


def test_limits_come_from_settings_and_the_service(tmp_path):
    client = build_test_client(tmp_path, max_video_upload_mb=7, max_pdf_upload_mb=3, max_concurrent_uploads_per_user=4)
    response = client.get("/api/uploads/limits", headers=_headers(_token(client)))
    assert response.status_code == 200
    assert response.json() == {
        "maxVideoBytes": 7 * 1024 * 1024,
        "maxPdfBytes": 3 * 1024 * 1024,
        "videoExtensions": sorted(VIDEO_EXTENSIONS),
        "maxConcurrentUploads": 4,
        "maxBatchSize": 50,
    }
    assert client.get("/api/uploads/limits").status_code == 401


def test_open_batch_answers_the_allowance(tmp_path):
    client = build_test_client(tmp_path)
    response = _open(client, _token(client), 5)
    assert response.status_code == 201
    body = response.json()
    assert (body["count"], body["remaining"]) == (5, 5)
    assert len(body["batchId"]) == 32 and body["expiresAt"].endswith("Z")


@pytest.mark.parametrize("count", [0, 1, 51, "3", 2.5, None, True, -4])
def test_open_batch_rejects_a_bad_count(tmp_path, count):
    client = build_test_client(tmp_path)
    assert _open(client, _token(client), count).status_code in {400, 422}


def test_max_count_follows_the_setting(tmp_path):
    client = build_test_client(tmp_path, max_upload_batch_size=4)
    token = _token(client)
    assert _open(client, token, 5).status_code == 422
    assert _open(client, token, 4).status_code == 201


def test_a_batch_costs_the_hourly_budget_once_not_per_initiate(tmp_path):
    client = build_test_client(tmp_path, rate_limit_rerun_per_hour=2, max_concurrent_uploads_per_user=10)
    token = _token(client)
    batch_id = _open(client, token, 4).json()["batchId"]  # charge 1 of 2
    assert [_initiate(client, token, batch_id).status_code for _ in range(4)] == [201] * 4
    assert _initiate(client, token).status_code == 201  # charge 2 of 2: the budget was untouched by the batch's initiates
    assert _initiate(client, token).status_code == 429


def test_an_exhausted_batch_is_refused_without_touching_the_hourly_budget(tmp_path):
    client = build_test_client(tmp_path, rate_limit_rerun_per_hour=2, max_concurrent_uploads_per_user=10)
    token = _token(client)
    batch_id = _open(client, token, 2).json()["batchId"]  # charge 1 of 2
    assert _initiate(client, token, batch_id).status_code == 201
    assert _initiate(client, token, batch_id).status_code == 201
    refused = _initiate(client, token, batch_id)
    assert refused.status_code == 409 and "no uploads left" in refused.json()["error"]
    assert _initiate(client, token).status_code == 201  # the one remaining unit of budget is still there


def test_another_accounts_batch_cannot_be_spent(tmp_path):
    client = build_test_client(tmp_path)
    admin = _token(client)
    other = _second_marker_token(client, admin)
    batch_id = _open(client, admin, 3).json()["batchId"]
    refused = _initiate(client, other, batch_id)
    assert refused.status_code == 409
    assert _initiate(client, admin, batch_id).status_code == 201


def test_an_unknown_batch_is_refused_not_charged_to_the_hourly_budget(tmp_path):
    client = build_test_client(tmp_path, rate_limit_rerun_per_hour=1)
    token = _token(client)
    assert _initiate(client, token, "0" * 32).status_code == 409
    assert _initiate(client, token, "").status_code == 409
    assert _initiate(client, token).status_code == 201  # budget untouched


def test_an_expired_batch_is_refused(tmp_path):
    client = build_test_client(tmp_path)
    token = _token(client)
    container = client.app.state.container
    batch_id = _open(client, token, 3).json()["batchId"]
    container.upload_batches._clock = lambda: 4_000_000_000.0
    refused = _initiate(client, token, batch_id)
    assert refused.status_code == 409 and "expired" in refused.json()["error"]


def test_open_batches_are_capped_over_http(tmp_path):
    client = build_test_client(tmp_path, max_open_upload_batches_per_user=1)
    token = _token(client)
    assert _open(client, token).status_code == 201
    assert _open(client, token).status_code == 429


def test_the_concurrent_uploads_cap_still_applies_and_does_not_burn_the_unit(tmp_path):
    client = build_test_client(tmp_path, max_concurrent_uploads_per_user=1)
    token = _token(client)
    batch_id = _open(client, token, 2).json()["batchId"]
    first = _initiate(client, token, batch_id)
    assert first.status_code == 201
    # In flight: refused, over and over, without using up the second unit.
    for _ in range(3):
        refused = _initiate(client, token, batch_id)
        assert refused.status_code == 429 and "in flight" in str(refused.json())
    assert client.delete(f"/api/uploads/{first.json()['uploadId']}", headers=_headers(token)).status_code == 200
    assert _initiate(client, token, batch_id).status_code == 201


def test_a_rejected_file_does_not_burn_the_unit_either(tmp_path):
    client = build_test_client(tmp_path, max_video_upload_mb=1, max_concurrent_uploads_per_user=10)
    token = _token(client)
    batch_id = _open(client, token, 2).json()["batchId"]
    oversized = {**UPLOAD, "files": [{**UPLOAD["files"][0], "sizeBytes": 2 * 1024 * 1024}, UPLOAD["files"][1]]}
    headers = {**_headers(token), "X-Upload-Batch": batch_id}
    assert client.post("/api/uploads/initiate", headers=headers, json=oversized).status_code == 413
    assert _initiate(client, token, batch_id).status_code == 201
    assert _initiate(client, token, batch_id).status_code == 201


def test_without_the_header_initiate_is_charged_as_before(tmp_path):
    client = build_test_client(tmp_path, rate_limit_rerun_per_hour=1, max_concurrent_uploads_per_user=10)
    token = _token(client)
    assert _initiate(client, token).status_code == 201
    assert _initiate(client, token).status_code == 429
