"""The Compute Engine calls the gate makes, over the REST API.

REST through ``google-auth``'s ``AuthorizedSession`` rather than the
``google-cloud-compute`` client: a handful of calls do not justify a large
generated dependency in a service whose cold start a person is waiting on.

The gate's service account needs exactly these permissions on the one
instance (deploy/gcp/README.md has the custom role): ``compute.instances.get``,
``compute.instances.start``, ``compute.instances.getGuestAttributes``,
``compute.zoneOperations.get`` and, for the GPU fallback
(``GATE_MACHINE_PROFILES``), ``compute.instances.setMachineType`` and
``compute.instances.setMachineResources``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

_API = "https://compute.googleapis.com/compute/v1"


@dataclass(frozen=True)
class InstanceRef:
    project: str
    zone: str
    name: str

    @property
    def base(self) -> str:
        return f"{_API}/projects/{self.project}/zones/{self.zone}"


# Start failures that mean "this GPU is not available here right now", as
# opposed to "something is wrong with the machine". Only these move a start on
# to the next machine profile.
STOCKOUT_CODES = frozenset({"ZONE_RESOURCE_POOL_EXHAUSTED", "ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS"})
QUOTA_CODES = frozenset({"QUOTA_EXCEEDED"})


def basename(url: str | None) -> str:
    """``.../zones/z/machineTypes/g2-standard-8`` -> ``g2-standard-8``."""
    return (url or "").rstrip("/").rsplit("/", 1)[-1]


@dataclass(frozen=True)
class InstanceState:
    status: str
    last_start_timestamp: str | None
    machine_type: str = ""
    # (accelerator type, count), e.g. (("nvidia-l4", 1),). A G2 machine type
    # lists its built-in L4 here too.
    accelerators: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class OperationResult:
    name: str
    done: bool
    error_codes: frozenset[str] = frozenset()
    error_message: str | None = None

    @property
    def failed(self) -> bool:
        return bool(self.error_codes or self.error_message)


class ComputeClient(Protocol):
    def get_instance(self) -> InstanceState: ...
    def start_instance(self) -> str: ...
    def get_boot_attribute(self) -> str | None: ...
    def operation_error(self, operation: str) -> str | None: ...
    def wait_operation(self, operation: str) -> OperationResult: ...
    def set_machine_type(self, machine_type: str) -> str: ...
    def set_accelerators(self, accelerators: tuple[tuple[str, int], ...]) -> str: ...


def describe_start_error(result: OperationResult) -> str | None:
    """A failed start operation in plain words, or None when it did not fail.

    The one failure worth naming to a person: a GPU machine can fail to start
    because the zone has no GPUs free right now (``ZONE_RESOURCE_POOL_EXHAUSTED``),
    which "try again later" fixes.
    """
    if not result.failed:
        return None
    if result.error_codes & STOCKOUT_CODES:
        return "Google Cloud has no GPU free in this zone right now. Please try again in a few minutes."
    if result.error_codes & QUOTA_CODES:
        return "The project's GPU quota is used up. Ask the administrator to raise it."
    return result.error_message or "The machine could not be started."


def _operation_result(name: str, body: dict[str, Any]) -> OperationResult:
    errors = ((body.get("error") or {}).get("errors")) or []
    return OperationResult(
        name=name,
        done=str(body.get("status", "")).upper() == "DONE",
        error_codes=frozenset(str(error.get("code", "")) for error in errors if error.get("code")),
        error_message=str(errors[0].get("message")) if errors and errors[0].get("message") else None,
    )


class RestComputeClient:
    def __init__(self, ref: InstanceRef, session: Any | None = None) -> None:
        self.ref = ref
        if session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/compute"])
            session = AuthorizedSession(credentials)
        self.session = session

    def _url(self, suffix: str = "") -> str:
        return f"{self.ref.base}/instances/{self.ref.name}{suffix}"

    def get_instance(self) -> InstanceState:
        response = self.session.get(self._url(), timeout=10)
        response.raise_for_status()
        body = response.json()
        return InstanceState(
            status=str(body.get("status", "")),
            last_start_timestamp=body.get("lastStartTimestamp"),
            machine_type=basename(body.get("machineType")),
            accelerators=tuple(
                (basename(item.get("acceleratorType")), int(item.get("acceleratorCount") or 0))
                for item in body.get("guestAccelerators") or []
            ),
        )

    def start_instance(self) -> str:
        response = self.session.post(self._url("/start"), timeout=15)
        response.raise_for_status()
        return str(response.json().get("name", ""))

    def get_boot_attribute(self) -> str | None:
        response = self.session.get(self._url("/getGuestAttributes"), params={"queryPath": "osce/boot"}, timeout=10)
        # 404: nothing written yet (or guest attributes are off) — a normal
        # answer for a machine that is still booting, not an error.
        if response.status_code in (400, 404):
            return None
        response.raise_for_status()
        items = (response.json().get("queryValue") or {}).get("items") or []
        for item in items:
            if item.get("key") == "boot":
                return str(item.get("value", ""))
        return None

    def operation_error(self, operation: str) -> str | None:
        """The start operation's error in plain words, or None while it is fine."""
        response = self.session.get(f"{self.ref.base}/operations/{operation}", timeout=10)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return describe_start_error(_operation_result(operation, response.json()))

    def wait_operation(self, operation: str) -> OperationResult:
        """Block until the operation is done, or Compute's own wait gives up
        (about two minutes) and answers with it still running."""
        response = self.session.post(f"{self.ref.base}/operations/{operation}/wait", timeout=150)
        response.raise_for_status()
        return _operation_result(operation, response.json())

    def set_machine_type(self, machine_type: str) -> str:
        response = self.session.post(
            self._url("/setMachineType"),
            json={"machineType": f"zones/{self.ref.zone}/machineTypes/{machine_type}"},
            timeout=15,
        )
        response.raise_for_status()
        return str(response.json().get("name", ""))

    def set_accelerators(self, accelerators: tuple[tuple[str, int], ...]) -> str:
        response = self.session.post(
            self._url("/setMachineResources"),
            json={
                "guestAccelerators": [
                    {"acceleratorType": f"zones/{self.ref.zone}/acceleratorTypes/{kind}", "acceleratorCount": count}
                    for kind, count in accelerators
                ]
            },
            timeout=15,
        )
        response.raise_for_status()
        return str(response.json().get("name", ""))
