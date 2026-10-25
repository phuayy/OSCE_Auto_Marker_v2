"""The four Compute Engine calls the gate makes, over the REST API.

REST through ``google-auth``'s ``AuthorizedSession`` rather than the
``google-cloud-compute`` client: four calls do not justify a large generated
dependency in a service whose cold start a person is waiting on.

The gate's service account needs exactly these permissions on the one
instance (deploy/gcp/README.md has the custom role): ``compute.instances.get``,
``compute.instances.start``, ``compute.instances.getGuestAttributes`` and
``compute.zoneOperations.get``.
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


@dataclass(frozen=True)
class InstanceState:
    status: str
    last_start_timestamp: str | None


class ComputeClient(Protocol):
    def get_instance(self) -> InstanceState: ...
    def start_instance(self) -> str: ...
    def get_boot_attribute(self) -> str | None: ...
    def operation_error(self, operation: str) -> str | None: ...


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
        return InstanceState(status=str(body.get("status", "")), last_start_timestamp=body.get("lastStartTimestamp"))

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
        """The start operation's error in plain words, or None while it is fine.

        The one failure worth naming to a person: a GPU machine can fail to
        start because the zone has no GPUs free right now
        (``ZONE_RESOURCE_POOL_EXHAUSTED``), which "try again later" fixes.
        """
        response = self.session.get(f"{self.ref.base}/operations/{operation}", timeout=10)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        errors = ((response.json().get("error") or {}).get("errors")) or []
        if not errors:
            return None
        codes = {str(error.get("code", "")) for error in errors}
        if codes & {"ZONE_RESOURCE_POOL_EXHAUSTED", "ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS"}:
            return "Google Cloud has no GPU free in this zone right now. Please try again in a few minutes."
        if "QUOTA_EXCEEDED" in codes:
            return "The project's GPU quota is used up. Ask the administrator to raise it."
        return str(errors[0].get("message") or "The machine could not be started.")
