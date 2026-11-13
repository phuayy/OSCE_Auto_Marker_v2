"""The Cloud Run start gate (deploy/gcp/gate): stages, sign-in, start.

The gate is its own small service, not part of the app; it lives here so CI
runs its tests with everything else.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

GATE_DIR = Path(__file__).resolve().parents[2] / "deploy" / "gcp" / "gate"
sys.path.insert(0, str(GATE_DIR))

from osce_gate.auth import COOKIE_NAME, Allowlist, SessionCookie  # noqa: E402
from osce_gate.compute import InstanceState  # noqa: E402
from osce_gate.history import MemoryHistory  # noqa: E402
from osce_gate.main import GateConfig, create_app  # noqa: E402
from osce_gate.stages import Observation, BootValue, checklist, derive_stage, describe, parse_boot_value  # noqa: E402

START = "2026-10-25T10:00:00.000-00:00"
START_TS = 1792922400.0  # START as unix seconds
SECRET = "s" * 40


class FakeCompute:
    def __init__(self, status: str = "TERMINATED") -> None:
        self.status = status
        self.boot: str | None = None
        self.started = 0
        self.op_error: str | None = None

    def get_instance(self) -> InstanceState:
        return InstanceState(status=self.status, last_start_timestamp=START)

    def start_instance(self) -> str:
        self.started += 1
        self.status = "PROVISIONING"
        return "operation-1"

    def get_boot_attribute(self) -> str | None:
        return self.boot

    def operation_error(self, operation: str) -> str | None:
        return self.op_error


def _config(**overrides) -> GateConfig:
    values = dict(
        project="p",
        zone="asia-southeast1-b",
        instance="osce",
        app_url="https://marker.example.org",
        probe_url="https://marker.example.org",
        google_client_id="client-id",
        allowlist=Allowlist.parse("dr.m@example.org", "school.edu"),
        session_secret=SECRET,
        cookie_secure=False,
    )
    values.update(overrides)
    return GateConfig(**values)


def _client(compute: FakeCompute, *, ready: bool = False, claims: dict | None = None, now: float = START_TS + 90):
    app = create_app(
        _config(),
        compute=compute,
        verify_token=lambda _credential: claims or {},
        history=MemoryHistory(),
        probe=lambda _url: ready,
        clock=lambda: now,
    )
    return TestClient(app)


# --- stages ------------------------------------------------------------------


def _obs(status: str, **kwargs) -> Observation:
    return Observation(instance_status=status, last_start=kwargs.pop("last_start", START_TS), boot=kwargs.pop("boot", None),
                       app_ready=kwargs.pop("app_ready", False), **kwargs)


@pytest.mark.parametrize(
    ("observation", "stage"),
    [
        (_obs("TERMINATED"), "stopped"),
        (_obs("TERMINATED", start_requested=True), "requested"),
        (_obs("STAGING"), "provisioning"),
        (_obs("PROVISIONING"), "provisioning"),
        (_obs("RUNNING"), "booting"),
        (_obs("RUNNING", boot=BootValue("database", START_TS + 40)), "database"),
        (_obs("RUNNING", boot=BootValue("loading", START_TS + 50)), "loading"),
        # The app said ready but does not answer yet: still "almost".
        (_obs("RUNNING", boot=BootValue("ready", START_TS + 60)), "loading"),
        (_obs("RUNNING", boot=BootValue("ready", START_TS + 60), app_ready=True), "ready"),
        # Left over from the previous run: ignored.
        (_obs("RUNNING", boot=BootValue("ready", START_TS - 3600)), "booting"),
        (_obs("STOPPING"), "stopping"),
        (_obs("REPAIRING"), "unavailable"),
        (_obs("TERMINATED", start_requested=True, start_error="no GPU"), "failed"),
    ],
)
def test_derive_stage(observation: Observation, stage: str) -> None:
    assert derive_stage(observation) == stage


def test_checklist_marks_done_current_pending() -> None:
    states = {step["key"]: step["state"] for step in checklist("database")}
    assert states["provisioning"] == "done"
    assert states["database"] == "current"
    assert states["ready"] == "pending"
    assert all(step["state"] == "done" for step in checklist("ready"))
    assert checklist("stopped") == []


def test_parse_boot_value() -> None:
    assert parse_boot_value("loading 1792922450") == BootValue("loading", 1792922450.0)
    assert parse_boot_value("garbage") is None
    assert parse_boot_value(None) is None


def test_estimate_uses_recent_starts() -> None:
    body = describe(_obs("STAGING"), now=START_TS + 30, history=[200, 260, 220], default_estimate_seconds=240)
    assert body["estimateSeconds"] == 220
    assert body["elapsedSeconds"] == 30
    assert body["starting"] is True
    assert describe(_obs("TERMINATED"), now=START_TS, history=[], default_estimate_seconds=240)["estimateSeconds"] == 240


# --- the service ---------------------------------------------------------------


def test_anyone_can_see_whether_it_is_running() -> None:
    compute = FakeCompute("RUNNING")
    body = _client(compute, ready=True).get("/api/status").json()
    assert body["stage"] == "ready"
    assert body["running"] is True
    assert body["appUrl"] == "https://marker.example.org"
    assert body["email"] is None


def test_start_needs_a_signed_in_allowlisted_account() -> None:
    compute = FakeCompute()
    client = _client(compute)
    assert client.post("/api/start").status_code == 401
    assert compute.started == 0


def test_sign_in_refuses_an_account_not_on_the_list() -> None:
    client = _client(FakeCompute(), claims={"email": "someone@gmail.com", "email_verified": True})
    response = client.post("/api/session", json={"credential": "token"})
    assert response.status_code == 403
    assert "not allowed" in response.json()["detail"]


def test_sign_in_refuses_an_unverified_email() -> None:
    client = _client(FakeCompute(), claims={"email": "dr.m@example.org", "email_verified": False})
    assert client.post("/api/session", json={"credential": "token"}).status_code == 403


def test_signed_in_start_then_watch_the_stages() -> None:
    compute = FakeCompute()
    client = _client(compute, claims={"email": "Prof.X@school.edu", "email_verified": True})
    assert client.post("/api/session", json={"credential": "token"}).json() == {"email": "prof.x@school.edu"}

    started = client.post("/api/start").json()
    assert started["started"] is True and started["operation"] == "operation-1"
    assert compute.started == 1
    # A second press while it is already starting is not a second start.
    assert client.post("/api/start").json()["started"] is False
    assert compute.started == 1

    status = client.get("/api/status", params={"operation": "operation-1"}).json()
    assert status["stage"] == "provisioning"
    assert status["email"] == "prof.x@school.edu"

    compute.status = "RUNNING"
    compute.boot = f"database {int(START_TS) + 45}"
    status = client.get("/api/status", params={"operation": "operation-1"}).json()
    assert status["stage"] == "database"
    assert status["elapsedSeconds"] == 90


def test_a_failed_start_says_why() -> None:
    compute = FakeCompute()
    compute.op_error = "Google Cloud has no GPU free in this zone right now."
    body = _client(compute).get("/api/status", params={"operation": "op"}).json()
    assert body["stage"] == "failed"
    assert "no GPU" in body["error"]


def test_ready_records_how_long_the_start_took() -> None:
    compute = FakeCompute("RUNNING")
    compute.boot = f"ready {int(START_TS) + 180}"
    history = MemoryHistory()
    app = create_app(_config(), compute=compute, verify_token=lambda _c: {}, history=history,
                     probe=lambda _u: True, clock=lambda: START_TS + 200)
    TestClient(app).get("/api/status")
    assert history.durations() == [180.0]


def test_cookie_is_signed_and_rechecked_against_the_allowlist() -> None:
    allow = Allowlist.parse("a@x.org", "")
    cookies = SessionCookie(SECRET, clock=lambda: 1000.0)
    value = cookies.issue("a@x.org")
    assert cookies.read(value, allow) == "a@x.org"
    assert cookies.read(value[:-2] + "xx", allow) is None
    assert cookies.read(value, Allowlist.parse("b@x.org", "")) is None
    expired = SessionCookie(SECRET, clock=lambda: 1000.0 + 13 * 3600)
    assert expired.read(value, allow) is None


def test_the_page_is_served() -> None:
    response = _client(FakeCompute()).get("/")
    assert response.status_code == 200
    assert "Start the marker" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert COOKIE_NAME == "osce_gate"


# --- GPU fallback (GATE_MACHINE_PROFILES) -------------------------------------

from osce_gate.compute import OperationResult  # noqa: E402
from osce_gate.hardware import MachineProfile, gpu_label, parse_profiles, start_preferred  # noqa: E402

L4_THEN_T4 = parse_profiles("g2-standard-8, n1-standard-8+nvidia-tesla-t4")


class ShapeShiftingCompute(FakeCompute):
    """A VM whose machine type can be changed while stopped, in a zone that
    has GPUs of some kinds free and not others."""

    def __init__(self, machine_type: str = "g2-standard-8", accelerators=(("nvidia-l4", 1),),
                 free: set[str] | None = None, quota_out: set[str] | None = None) -> None:
        super().__init__()
        self.machine_type = machine_type
        self.accelerators = tuple(accelerators)
        self.free = free if free is not None else {"NVIDIA L4", "NVIDIA T4"}
        self.quota_out = quota_out or set()
        self.calls: list[str] = []
        self.results: dict[str, OperationResult] = {}

    def _gpu(self) -> str:
        return gpu_label(self.machine_type, self.accelerators)

    def get_instance(self) -> InstanceState:
        return InstanceState(status=self.status, last_start_timestamp=START,
                             machine_type=self.machine_type, accelerators=self.accelerators)

    def _op(self, name: str, *, codes=frozenset()) -> str:
        self.results[name] = OperationResult(name=name, done=True, error_codes=frozenset(codes))
        return name

    def start_instance(self) -> str:
        assert self.status == "TERMINATED", "started a VM that was not stopped"
        self.started += 1
        name = f"start-{self.started}"
        self.calls.append(f"start:{self._gpu()}")
        if self._gpu() in self.quota_out:
            return self._op(name, codes={"QUOTA_EXCEEDED"})
        if self._gpu() not in self.free:
            return self._op(name, codes={"ZONE_RESOURCE_POOL_EXHAUSTED"})
        self.status = "PROVISIONING"
        return self._op(name)

    def wait_operation(self, operation: str) -> OperationResult:
        return self.results[operation]

    def set_machine_type(self, machine_type: str) -> str:
        assert self.status == "TERMINATED"
        if machine_type.startswith("g2-") and any(kind != "nvidia-l4" for kind, _ in self.accelerators):
            return self._op(f"type-{len(self.calls)}", codes={"INVALID_FIELD_VALUE"})
        self.calls.append(f"type:{machine_type}")
        self.machine_type = machine_type
        # A G2's L4 comes with the type; leaving G2 drops it.
        if machine_type.startswith("g2-"):
            self.accelerators = (("nvidia-l4", 1),)
        elif any(kind == "nvidia-l4" for kind, _ in self.accelerators):
            self.accelerators = ()
        return self._op(f"type-{len(self.calls)}")

    def set_accelerators(self, accelerators) -> str:
        assert self.status == "TERMINATED"
        self.calls.append(f"gpu:{','.join(kind for kind, _ in accelerators) or 'none'}")
        self.accelerators = tuple(accelerators)
        return self._op(f"gpu-{len(self.calls)}")


def test_profiles_parse() -> None:
    l4, t4 = L4_THEN_T4
    assert l4 == MachineProfile("g2-standard-8") and l4.label == "NVIDIA L4"
    assert t4 == MachineProfile("n1-standard-8", (("nvidia-tesla-t4", 1),)) and t4.label == "NVIDIA T4"
    assert MachineProfile.parse("n1-standard-16+nvidia-tesla-t4x2").accelerators == (("nvidia-tesla-t4", 2),)
    assert parse_profiles("") == ()
    with pytest.raises(ValueError):
        MachineProfile.parse("g2-standard-8+nvidia-tesla-t4")


def test_l4_free_starts_on_l4_without_reshaping() -> None:
    compute = ShapeShiftingCompute()
    outcome = start_preferred(compute, L4_THEN_T4, sleep=lambda _s: None)
    assert outcome.started and outcome.profile.label == "NVIDIA L4" and outcome.skipped == []
    assert compute.calls == ["start:NVIDIA L4"]


def test_no_l4_falls_back_to_t4() -> None:
    compute = ShapeShiftingCompute(free={"NVIDIA T4"})
    outcome = start_preferred(compute, L4_THEN_T4, sleep=lambda _s: None)
    assert outcome.started and outcome.profile.label == "NVIDIA T4"
    assert outcome.skipped == [("NVIDIA L4", "none free")]
    assert compute.calls == ["start:NVIDIA L4", "type:n1-standard-8", "gpu:nvidia-tesla-t4", "start:NVIDIA T4"]


def test_next_start_goes_back_to_l4_first() -> None:
    compute = ShapeShiftingCompute("n1-standard-8", (("nvidia-tesla-t4", 1),))
    outcome = start_preferred(compute, L4_THEN_T4, sleep=lambda _s: None)
    assert outcome.profile.label == "NVIDIA L4"
    # The T4 is detached before the type change: a G2 cannot carry one.
    assert compute.calls == ["gpu:none", "type:g2-standard-8", "start:NVIDIA L4"]


def test_l4_quota_also_falls_back() -> None:
    compute = ShapeShiftingCompute(quota_out={"NVIDIA L4"})
    outcome = start_preferred(compute, L4_THEN_T4, sleep=lambda _s: None)
    assert outcome.profile.label == "NVIDIA T4"
    assert outcome.skipped == [("NVIDIA L4", "quota used up")]


def test_nothing_free_says_what_was_tried() -> None:
    compute = ShapeShiftingCompute(free=set())
    outcome = start_preferred(compute, L4_THEN_T4, zone="asia-southeast1-b", sleep=lambda _s: None)
    assert not outcome.started
    assert "no GPU free in asia-southeast1-b" in outcome.error
    assert "NVIDIA L4, NVIDIA T4" in outcome.error


def test_other_failures_do_not_fall_back() -> None:
    compute = ShapeShiftingCompute()
    compute.start_instance = lambda: "bad"  # type: ignore[method-assign]
    compute.results["bad"] = OperationResult(name="bad", done=True, error_codes=frozenset({"RESOURCE_NOT_FOUND"}),
                                             error_message="The boot disk is missing.")
    outcome = start_preferred(compute, L4_THEN_T4, sleep=lambda _s: None)
    assert not outcome.started and outcome.error == "The boot disk is missing."


def test_start_endpoint_falls_back_and_status_says_so() -> None:
    compute = ShapeShiftingCompute(free={"NVIDIA T4"})
    app = create_app(_config(machine_profiles=L4_THEN_T4), compute=compute,
                     verify_token=lambda _c: {"email": "dr.m@example.org", "email_verified": True},
                     history=MemoryHistory(), probe=lambda _u: False, clock=lambda: START_TS + 30)
    client = TestClient(app)
    client.post("/api/session", json={"credential": "token"})
    body = client.post("/api/start").json()
    assert body["started"] is True and body["gpu"] == "NVIDIA T4"
    assert body["skipped"] == [{"gpu": "NVIDIA L4", "reason": "none free"}]
    status = client.get("/api/status", params={"operation": body["operation"]}).json()
    assert status["gpu"] == "NVIDIA T4" and status["fallbackGpu"] is True and status["preferredGpu"] == "NVIDIA L4"


def test_start_endpoint_reports_when_no_gpu_is_free() -> None:
    compute = ShapeShiftingCompute(free=set())
    app = create_app(_config(machine_profiles=L4_THEN_T4), compute=compute,
                     verify_token=lambda _c: {"email": "dr.m@example.org", "email_verified": True},
                     history=MemoryHistory(), probe=lambda _u: False)
    client = TestClient(app)
    client.post("/api/session", json={"credential": "token"})
    response = client.post("/api/start")
    assert response.status_code == 503
    assert "tried NVIDIA L4, NVIDIA T4" in response.json()["detail"]
