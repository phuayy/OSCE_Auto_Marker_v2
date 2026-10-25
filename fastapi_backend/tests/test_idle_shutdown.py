"""On-demand hosting: what counts as activity, when the VM switches itself
off, and what the start gate is told about the boot."""

from __future__ import annotations

import asyncio
import sys

import httpx

from app.core.activity import ActivityTracker, counts_as_activity
from app.core.boot_stage import BootStage, BootStageReporter, encode_boot_value
from app.domain.jobs import IN_PROGRESS_JOB_STATUSES
from app.domain.sessions import SessionStatus
from app.services.idle_shutdown_service import IdleShutdownService


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeCounter:
    def __init__(self) -> None:
        self.counts: dict[frozenset[str], int] = {}
        self.asked: list[frozenset[str]] = []

    async def count_in_statuses(self, statuses: frozenset[str]) -> int:
        self.asked.append(statuses)
        return self.counts.get(statuses, 0)


class RecordingReporter(BootStageReporter):
    def __init__(self) -> None:
        super().__init__(enabled=False)
        self.stages: list[BootStage] = []

    async def report(self, stage: BootStage) -> None:
        self.stages.append(stage)


def _service(clock: FakeClock, *, command: str | None = None, minutes: int = 30):
    jobs, sessions, reporter = FakeCounter(), FakeCounter(), RecordingReporter()
    service = IdleShutdownService(
        idle_minutes=minutes,
        check_interval_seconds=60,
        command=command or f'"{sys.executable}" -c "pass"',
        activity=ActivityTracker(clock),
        jobs=jobs,
        sessions=sessions,
        boot_stage=reporter,
    )
    return service, jobs, sessions, reporter


def test_a_tab_polling_on_its_own_is_not_activity() -> None:
    assert not counts_as_activity("GET", "/api/notifications")
    assert not counts_as_activity("GET", "/api/events")
    assert not counts_as_activity("GET", "/api/events/versions")
    assert not counts_as_activity("GET", "/api/health/ready")
    assert not counts_as_activity("GET", "/api/auth/stream-ticket")
    assert not counts_as_activity("OPTIONS", "/api/sessions")


def test_a_person_acting_is_activity() -> None:
    assert counts_as_activity("GET", "/api/sessions")
    assert counts_as_activity("GET", "/media/videos/x.mp4")
    assert counts_as_activity("PUT", "/api/uploads/u1/parts/3")
    # Marking notifications read is a click, not the bell's poll.
    assert counts_as_activity("POST", "/api/notifications/read-all")
    # A prefix match must be on a path boundary.
    assert counts_as_activity("GET", "/api/eventsummary")


def test_disabled_never_shuts_down() -> None:
    clock = FakeClock()
    service, *_ = _service(clock, minutes=0)
    clock.now += 10 * 24 * 3600
    assert not service.enabled
    assert asyncio.run(service.check_once()) is False


def test_shuts_down_once_the_window_passes_with_nothing_in_flight() -> None:
    clock = FakeClock()
    service, _jobs, _sessions, reporter = _service(clock)
    clock.now += 29 * 60
    assert asyncio.run(service.check_once()) is False
    clock.now += 61
    assert asyncio.run(service.check_once()) is True
    assert reporter.stages == [BootStage.STOPPING]
    # Issued once: later ticks do not run the command again.
    clock.now += 3600
    assert asyncio.run(service.check_once()) is False


def test_a_request_restarts_the_window() -> None:
    clock = FakeClock()
    service, *_ = _service(clock)
    clock.now += 25 * 60
    service.activity.record()
    clock.now += 25 * 60
    assert asyncio.run(service.check_once()) is False


def test_work_in_flight_holds_the_vm_and_restarts_the_window_when_it_ends() -> None:
    clock = FakeClock()
    service, jobs, sessions, _ = _service(clock)
    jobs.counts[IN_PROGRESS_JOB_STATUSES] = 1
    clock.now += 3 * 3600
    assert asyncio.run(service.check_once()) is False
    # The last job finished just now: a full window from here, not from boot.
    jobs.counts.clear()
    clock.now += 20 * 60
    assert asyncio.run(service.check_once()) is False

    sessions.counts[frozenset({SessionStatus.ASSEMBLING})] = 1
    clock.now += 3600
    assert asyncio.run(service.check_once()) is False


def test_a_failing_command_is_retried_next_tick() -> None:
    clock = FakeClock()
    service, *_ = _service(clock, command=f'"{sys.executable}" -c "raise SystemExit(1)"')
    clock.now += 31 * 60
    assert asyncio.run(service.check_once()) is False
    service.command = f'"{sys.executable}" -c "pass"'
    assert asyncio.run(service.check_once()) is True


def test_state_reports_the_countdown() -> None:
    clock = FakeClock()
    service, jobs, *_ = _service(clock)
    clock.now += 600
    state = asyncio.run(service.state()).as_dict()
    assert state == {"enabled": True, "busy": False, "idleSeconds": 600, "thresholdSeconds": 1800}
    jobs.counts[IN_PROGRESS_JOB_STATUSES] = 2
    assert asyncio.run(service.state()).as_dict()["idleSeconds"] == 0


def test_boot_stage_is_written_as_one_guest_attribute() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    reporter = BootStageReporter(True, clock=lambda: 1729850000.7, transport=httpx.MockTransport(handler))
    asyncio.run(reporter.report(BootStage.DATABASE))
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "PUT"
    assert request.url.path.endswith("/guest-attributes/osce/boot")
    assert request.headers["Metadata-Flavor"] == "Google"
    assert request.content == b"database 1729850000"
    assert encode_boot_value(BootStage.READY, 5.0) == "ready 5"


def test_boot_stage_failure_never_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no metadata server here")

    reporter = BootStageReporter(True, transport=httpx.MockTransport(handler))
    asyncio.run(reporter.report(BootStage.LOADING))
    asyncio.run(reporter.report(BootStage.READY))


def test_boot_stage_disabled_sends_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("disabled reporter made a request")

    asyncio.run(BootStageReporter(False, transport=httpx.MockTransport(handler)).report(BootStage.READY))


def test_requests_through_the_app_drive_the_tracker(tmp_path) -> None:
    from tests.test_routes import build_test_client

    clock = FakeClock()
    client = build_test_client(tmp_path, idle_shutdown_minutes=30)
    container = client.app.state.container
    container.activity._clock = clock
    container.activity.record()
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    clock.now += 600
    # An open tab polling on its own does not restart the window.
    client.get("/api/notifications", headers=headers)
    assert container.activity.idle_seconds() == 600
    # Anonymous traffic does not either.
    client.get("/api/sessions")
    assert container.activity.idle_seconds() == 600
    # A person acting does.
    client.get("/api/sessions", headers=headers)
    assert container.activity.idle_seconds() == 0

    clock.now += 120
    diagnostics = client.get("/api/admin/health/diagnostics", headers=headers).json()
    assert diagnostics["idleShutdown"]["enabled"] is True
    assert diagnostics["idleShutdown"]["thresholdSeconds"] == 1800
