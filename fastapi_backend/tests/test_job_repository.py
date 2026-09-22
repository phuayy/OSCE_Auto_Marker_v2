from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.database.orm import OrmDatabase
from app.repositories.job_repository import JobRepository


def test_job_repository_recovers_running_jobs(tmp_path) -> None:
    repository = JobRepository(OrmDatabase(tmp_path / "osce_marker.sqlite3"))
    job = {
        "id": "job-1",
        "sessionId": "session-1",
        "taskType": "process_session",
        "status": "running",
        "attempts": 1,
        "maxAttempts": 3,
        "createdAt": "2026-01-01T00:00:00Z",
        "queuedAt": "2026-01-01T00:00:01Z",
        "startedAt": "2026-01-01T00:00:02Z",
        "endedAt": None,
        "error": None,
        "payload": {"workflow": "standard"},
    }

    asyncio.run(repository.write(job))
    recovered = asyncio.run(repository.recover_interrupted_jobs())

    assert len(recovered) == 1
    assert recovered[0]["id"] == "job-1"
    assert recovered[0]["status"] == "queued"
    assert recovered[0]["startedAt"] is None
    assert recovered[0]["requeuedAt"] is not None


def test_job_repository_claims_queued_job_once(tmp_path) -> None:
    repository = JobRepository(OrmDatabase(tmp_path / "osce_marker.sqlite3"))
    job = {
        "id": "job-1",
        "sessionId": "session-1",
        "taskType": "process_session",
        "status": "queued",
        "attempts": 0,
        "maxAttempts": 3,
        "createdAt": "2026-01-01T00:00:00Z",
        "queuedAt": "2026-01-01T00:00:01Z",
        "startedAt": None,
        "endedAt": None,
        "error": None,
        "payload": {"workflow": "standard"},
    }

    asyncio.run(repository.write(job))
    first_claim = asyncio.run(repository.claim_queued("job-1", "worker-1"))
    second_claim = asyncio.run(repository.claim_queued("job-1", "worker-2"))

    assert first_claim.claimed
    assert first_claim.job["status"] == "running"
    assert first_claim.job["attempts"] == 1
    assert not second_claim.claimed
    assert second_claim.outcome == "not_queued"


def test_job_repository_prepares_failed_job_for_hatchet_retry(tmp_path) -> None:
    repository = JobRepository(OrmDatabase(tmp_path / "osce_marker.sqlite3"))
    job = {
        "id": "job-1",
        "sessionId": "session-1",
        "taskType": "process_session",
        "status": "failed",
        "attempts": 1,
        "maxAttempts": 3,
        "createdAt": "2026-01-01T00:00:00Z",
        "queuedAt": "2026-01-01T00:00:01Z",
        "startedAt": "2026-01-01T00:00:02Z",
        "endedAt": "2026-01-01T00:00:03Z",
        "error": "transient",
        "payload": {"workflow": "standard"},
    }

    asyncio.run(repository.write(job))
    prepared = asyncio.run(repository.prepare_retry_attempt("job-1", "retry"))

    assert prepared["status"] == "queued"
    assert prepared["attempts"] == 1
    assert prepared["startedAt"] is None
    assert prepared["endedAt"] is None
    assert prepared["error"] == "retry"


def test_job_repository_does_not_requeue_cancelled_interrupted_job(tmp_path) -> None:
    repository = JobRepository(OrmDatabase(tmp_path / "osce_marker.sqlite3"))
    job = {
        "id": "job-1",
        "sessionId": "session-1",
        "taskType": "process_session",
        "status": "cancelled",
        "attempts": 1,
        "maxAttempts": 3,
        "createdAt": "2026-01-01T00:00:00Z",
        "queuedAt": "2026-01-01T00:00:01Z",
        "startedAt": "2026-01-01T00:00:02Z",
        "endedAt": "2026-01-01T00:00:03Z",
        "error": "cancelled",
        "payload": {"workflow": "standard"},
    }

    asyncio.run(repository.write(job))
    requeued = asyncio.run(repository.requeue_interrupted_job("job-1", "interrupted"))

    assert requeued["status"] == "cancelled"
    assert requeued["error"] == "cancelled"


# --- heartbeat / stale-job reaper (A7) --------------------------------------


def _running_job(job_id: str = "job-1", *, locked_by: str = "worker-1", locked_at: str) -> dict:
    return {
        "id": job_id,
        "sessionId": "session-1",
        "taskType": "process_session",
        "status": "running",
        "attempts": 1,
        "maxAttempts": 3,
        "createdAt": "2026-01-01T00:00:00Z",
        "queuedAt": "2026-01-01T00:00:01Z",
        "startedAt": "2026-01-01T00:00:02Z",
        "endedAt": None,
        "error": None,
        "lockedBy": locked_by,
        "lockedAt": locked_at,
        "payload": {"workflow": "standard"},
    }


def test_touch_heartbeat_refreshes_locked_at_for_the_matching_worker(tmp_path) -> None:
    repository = JobRepository(OrmDatabase(tmp_path / "osce_marker.sqlite3"))
    asyncio.run(repository.write(_running_job(locked_at="2026-01-01T00:00:02Z")))

    touched = asyncio.run(repository.touch_heartbeat("job-1", "worker-1"))

    assert touched is True
    refreshed = asyncio.run(repository.read("job-1"))
    assert refreshed["lockedAt"] > "2026-01-01T00:00:02Z"


def test_touch_heartbeat_rejects_a_different_worker(tmp_path) -> None:
    """A heartbeat from a worker that no longer owns the row (reclaimed,
    reaped) must not resurrect its claim."""
    repository = JobRepository(OrmDatabase(tmp_path / "osce_marker.sqlite3"))
    asyncio.run(repository.write(_running_job(locked_by="worker-1", locked_at="2026-01-01T00:00:02Z")))

    touched = asyncio.run(repository.touch_heartbeat("job-1", "worker-2"))

    assert touched is False
    unchanged = asyncio.run(repository.read("job-1"))
    assert unchanged["lockedAt"] == "2026-01-01T00:00:02Z"


def test_touch_heartbeat_rejects_a_non_running_job(tmp_path) -> None:
    repository = JobRepository(OrmDatabase(tmp_path / "osce_marker.sqlite3"))
    job = _running_job(locked_at="2026-01-01T00:00:02Z")
    job["status"] = "succeeded"
    asyncio.run(repository.write(job))

    assert asyncio.run(repository.touch_heartbeat("job-1", "worker-1")) is False


def test_list_stale_running_finds_only_rows_past_the_cutoff(tmp_path) -> None:
    repository = JobRepository(OrmDatabase(tmp_path / "osce_marker.sqlite3"))
    stale_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    fresh_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    asyncio.run(repository.write(_running_job("stale-job", locked_at=stale_at)))
    asyncio.run(repository.write(_running_job("fresh-job", locked_at=fresh_at)))

    stale = asyncio.run(repository.list_stale_running(older_than_seconds=60))

    assert [job["id"] for job in stale] == ["stale-job"]


def test_list_stale_running_ignores_non_running_rows(tmp_path) -> None:
    repository = JobRepository(OrmDatabase(tmp_path / "osce_marker.sqlite3"))
    stale_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    job = _running_job(locked_at=stale_at)
    job["status"] = "failed"
    asyncio.run(repository.write(job))

    assert asyncio.run(repository.list_stale_running(older_than_seconds=60)) == []


# --- B2: a terminal status is first-writer-wins ------------------------------


def _queued_job(job_id: str = "job-1") -> dict:
    return {
        "id": job_id,
        "sessionId": "session-1",
        "taskType": "process_session",
        "status": "queued",
        "attempts": 0,
        "maxAttempts": 3,
        "createdAt": "2026-01-01T00:00:00Z",
        "queuedAt": "2026-01-01T00:00:01Z",
        "startedAt": None,
        "endedAt": None,
        "error": None,
        "payload": {"workflow": "standard"},
    }


def test_a_cancelled_job_cannot_be_overwritten_by_a_late_success(tmp_path) -> None:
    """A job the user cancelled must stay cancelled even if the engine run it
    could not fully abort reports success afterwards — the race B2 covers."""
    repository = JobRepository(OrmDatabase(tmp_path / "osce_marker.sqlite3"))
    asyncio.run(repository.write(_queued_job()))
    claim = asyncio.run(repository.claim_queued("job-1", "worker-1"))
    assert claim.claimed

    cancelled = asyncio.run(repository.mark_cancelled("job-1", "Session deleted."))
    assert cancelled["status"] == "cancelled"

    # The engine run's own completion callback lands after the cancel.
    late_success = asyncio.run(repository.mark_succeeded("job-1"))
    assert late_success["status"] == "cancelled"
    assert late_success["error"] == "Session deleted."

    on_disk = asyncio.run(repository.read("job-1"))
    assert on_disk["status"] == "cancelled"


def test_a_cancelled_job_cannot_be_overwritten_by_a_late_failure(tmp_path) -> None:
    repository = JobRepository(OrmDatabase(tmp_path / "osce_marker.sqlite3"))
    asyncio.run(repository.write(_queued_job()))
    asyncio.run(repository.claim_queued("job-1", "worker-1"))
    asyncio.run(repository.mark_cancelled("job-1", "Session deleted."))

    late_failure = asyncio.run(repository.mark_failed("job-1", "boom"))

    assert late_failure["status"] == "cancelled"


def test_a_finished_job_cannot_be_cancelled_after_the_fact(tmp_path) -> None:
    """Symmetric to the case above: once succeeded, a stray cancel must not
    relabel a result the user already saw."""
    repository = JobRepository(OrmDatabase(tmp_path / "osce_marker.sqlite3"))
    asyncio.run(repository.write(_queued_job()))
    asyncio.run(repository.claim_queued("job-1", "worker-1"))
    asyncio.run(repository.mark_succeeded("job-1"))

    late_cancel = asyncio.run(repository.mark_cancelled("job-1", "too late"))

    assert late_cancel["status"] == "succeeded"


# --- B1: SQLite read-then-write transactions take the write lock up front ---


def test_finish_takes_the_write_lock_before_reading(tmp_path) -> None:
    """``ensure_write_locked`` must run before the row is read, not after —
    reading first is exactly the deferred-BEGIN window that lets a concurrent
    commit produce SQLITE_BUSY_SNAPSHOT on the later write (see the
    JobRepository class docstring)."""
    repository = JobRepository(OrmDatabase(tmp_path / "osce_marker.sqlite3"))
    asyncio.run(repository.write(_queued_job()))
    asyncio.run(repository.claim_queued("job-1", "worker-1"))

    calls: list[str] = []
    original_ensure_write_locked = repository.database.ensure_write_locked
    original_require = JobRepository._require

    async def spy_ensure_write_locked(session):
        calls.append("lock")
        await original_ensure_write_locked(session)

    async def spy_require(db, job_id):
        calls.append("read")
        return await original_require(db, job_id)

    repository.database.ensure_write_locked = spy_ensure_write_locked  # type: ignore[method-assign]
    JobRepository._require = staticmethod(spy_require)
    try:
        asyncio.run(repository.mark_succeeded("job-1"))
    finally:
        JobRepository._require = staticmethod(original_require)

    assert calls == ["lock", "read"]


# --- F1: dispatch metadata writes never touch the claim columns -------------


def test_record_dispatch_keeps_a_concurrent_claim_intact(tmp_path) -> None:
    """The dispatcher's write lands after a worker has already claimed the
    row: only the payload may change — status, lock and attempts are the
    worker's."""
    repository = JobRepository(OrmDatabase(tmp_path / "osce_marker.sqlite3"))
    asyncio.run(repository.write(_queued_job()))
    claim = asyncio.run(repository.claim_queued("job-1", "worker-1"))
    assert claim.claimed

    recorded = asyncio.run(repository.record_dispatch("job-1", {"runId": "run-1", "dispatchedAt": "2026-01-01T00:00:03Z"}))

    assert recorded is not None
    assert recorded["status"] == "running"
    assert recorded["lockedBy"] == "worker-1"
    assert recorded["lockedAt"] == claim.job["lockedAt"]
    assert recorded["attempts"] == 1
    assert recorded["payload"] == {"workflow": "standard", "hatchet": {"runId": "run-1", "dispatchedAt": "2026-01-01T00:00:03Z"}}


def test_record_dispatch_returns_none_for_a_missing_row_instead_of_recreating_it(tmp_path) -> None:
    repository = JobRepository(OrmDatabase(tmp_path / "osce_marker.sqlite3"))
    asyncio.run(repository.initialize())

    assert asyncio.run(repository.record_dispatch("job-missing", {"runId": "run-1"})) is None
    assert asyncio.run(repository.exists("job-missing")) is False


def test_clear_dispatch_if_queued_strips_metadata_from_a_queued_row(tmp_path) -> None:
    repository = JobRepository(OrmDatabase(tmp_path / "osce_marker.sqlite3"))
    job = _queued_job()
    job["payload"] = {"workflow": "standard", "hatchet": {"runId": "run-old"}, "hatchetRunId": "run-legacy"}
    asyncio.run(repository.write(job))

    cleared = asyncio.run(repository.clear_dispatch_if_queued("job-1"))

    assert cleared is not None
    assert cleared["status"] == "queued"
    assert cleared["payload"] == {"workflow": "standard"}


def test_clear_dispatch_if_queued_refuses_a_claimed_row(tmp_path) -> None:
    """A row that was claimed between the caller's read and this write is no
    longer the caller's to redispatch: nothing is written and ``None`` says so."""
    repository = JobRepository(OrmDatabase(tmp_path / "osce_marker.sqlite3"))
    job = _queued_job()
    job["payload"] = {"workflow": "standard", "hatchet": {"runId": "run-live"}}
    asyncio.run(repository.write(job))
    asyncio.run(repository.claim_queued("job-1", "worker-1"))

    assert asyncio.run(repository.clear_dispatch_if_queued("job-1")) is None
    untouched = asyncio.run(repository.read("job-1"))
    assert untouched["status"] == "running"
    assert untouched["lockedBy"] == "worker-1"
    assert untouched["payload"]["hatchet"] == {"runId": "run-live"}


def test_rerun_strips_dispatch_metadata_in_the_same_transaction(tmp_path) -> None:
    repository = JobRepository(OrmDatabase(tmp_path / "osce_marker.sqlite3"))
    job = _queued_job()
    job["status"] = "failed"
    job["attempts"] = 1
    job["payload"] = {"workflow": "standard", "hatchet": {"runId": "run-old"}}
    asyncio.run(repository.write(job))

    rerun = asyncio.run(repository.rerun("job-1"))

    assert rerun["status"] == "queued"
    assert rerun["attempts"] == 0
    assert rerun["payload"] == {"workflow": "standard"}
    assert asyncio.run(repository.read("job-1"))["payload"] == {"workflow": "standard"}


def _job(job_id: str, created_at: str) -> dict:
    return {
        "id": job_id,
        "sessionId": f"session-{job_id}",
        "taskType": "process_session",
        "status": "queued",
        "attempts": 0,
        "maxAttempts": 3,
        "createdAt": created_at,
        "queuedAt": created_at,
        "startedAt": None,
        "endedAt": None,
        "error": None,
        "payload": {},
    }


def test_read_page_orders_newest_first_with_id_as_the_tiebreak(tmp_path) -> None:
    """Two jobs created in the same instant must still produce a total order —
    otherwise a row can be skipped or repeated across a page boundary."""
    repository = JobRepository(OrmDatabase(tmp_path / "osce_marker.sqlite3"))
    same_instant = "2026-01-01T00:00:00Z"
    for job_id in ["a", "b", "c"]:
        asyncio.run(repository.write(_job(job_id, same_instant)))
    asyncio.run(repository.write(_job("d", "2026-01-02T00:00:00Z")))

    page = asyncio.run(repository.read_page(limit=10))

    # Newest timestamp first; ties broken by id, descending.
    assert [row["id"] for row in page] == ["d", "c", "b", "a"]


def test_read_page_keyset_survives_a_row_inserted_ahead_of_the_cursor(tmp_path) -> None:
    """The whole point of a keyset cursor over an offset: a row landing ahead
    of a page already handed out must not shift what the next page returns."""
    repository = JobRepository(OrmDatabase(tmp_path / "osce_marker.sqlite3"))
    for index, job_id in enumerate(["a", "b", "c"]):
        asyncio.run(repository.write(_job(job_id, f"2026-01-0{index + 1}T00:00:00Z")))

    first_page = asyncio.run(repository.read_page(limit=2))
    assert [row["id"] for row in first_page] == ["c", "b"]

    # A brand-new job lands "ahead" of the page already served.
    asyncio.run(repository.write(_job("new", "2026-02-01T00:00:00Z")))

    after = (first_page[-1]["createdAt"], first_page[-1]["id"])
    second_page = asyncio.run(repository.read_page(limit=2, after=after))
    assert [row["id"] for row in second_page] == ["a"]
