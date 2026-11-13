"""Which GPU machine to start, in order of preference, and the start that
walks down that list when Google Cloud has none of the first kind free.

A zone runs out of a GPU model from time to time (``ZONE_RESOURCE_POOL_EXHAUSTED``).
The VM's disk lives in one zone, but its machine type can be changed freely
while it is stopped. So instead of failing, a start tries each profile in
``GATE_MACHINE_PROFILES`` in turn, re-shaping the stopped VM to that profile
before each attempt:

    GATE_MACHINE_PROFILES=g2-standard-8, n1-standard-8+nvidia-tesla-t4

means "an L4 machine if there is one, otherwise an N1 machine with a T4".
Every start begins with the first profile again, so a VM that fell back to a
T4 yesterday goes back to an L4 today when one is free.

Only a stockout or a GPU quota refusal moves on to the next profile. Any other
failure is the machine's own problem and is reported as it is.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from osce_gate.compute import (
    QUOTA_CODES,
    STOCKOUT_CODES,
    ComputeClient,
    InstanceState,
    OperationResult,
    describe_start_error,
)

logger = logging.getLogger(__name__)

_STOPPED = {"TERMINATED", "STOPPED", "SUSPENDED"}
# Machine series whose GPU comes with the machine type. Their accelerators
# cannot be changed with setMachineResources; changing the type changes them.
_BUILT_IN_GPU_SERIES = ("a2-", "a3-", "a4-", "g2-", "g4-")
_GPU_NAMES = {
    "nvidia-l4": "NVIDIA L4",
    "nvidia-tesla-t4": "NVIDIA T4",
    "nvidia-tesla-v100": "NVIDIA V100",
    "nvidia-tesla-p4": "NVIDIA P4",
    "nvidia-tesla-p100": "NVIDIA P100",
    "nvidia-tesla-a100": "NVIDIA A100",
}
_SERIES_GPU = {"g2-": "NVIDIA L4", "a2-": "NVIDIA A100", "a3-": "NVIDIA H100"}


def has_built_in_gpu(machine_type: str) -> bool:
    return machine_type.startswith(_BUILT_IN_GPU_SERIES)


def gpu_label(machine_type: str, accelerators: tuple[tuple[str, int], ...]) -> str:
    """``NVIDIA L4`` for a G2 machine, ``NVIDIA T4`` for an N1 with a T4, ``""`` for none."""
    for kind, count in accelerators:
        if count > 0:
            name = _GPU_NAMES.get(kind, kind)
            return name if count == 1 else f"{count} x {name}"
    for prefix, name in _SERIES_GPU.items():
        if machine_type.startswith(prefix):
            return name
    return ""


@dataclass(frozen=True)
class MachineProfile:
    machine_type: str
    # Attached with setMachineResources; empty for a series with a built-in GPU.
    accelerators: tuple[tuple[str, int], ...] = ()

    @property
    def label(self) -> str:
        return gpu_label(self.machine_type, self.accelerators) or self.machine_type

    def matches(self, instance: InstanceState) -> bool:
        if instance.machine_type != self.machine_type:
            return False
        if has_built_in_gpu(self.machine_type):
            return True
        return sorted(a for a in instance.accelerators if a[1] > 0) == sorted(self.accelerators)

    @classmethod
    def parse(cls, spec: str) -> "MachineProfile":
        """``g2-standard-8`` or ``n1-standard-8+nvidia-tesla-t4`` (``...t4x2`` for two)."""
        machine_type, _, accelerator = spec.strip().partition("+")
        machine_type = machine_type.strip()
        if not machine_type:
            raise ValueError(f"Machine profile {spec!r} has no machine type.")
        accelerators: tuple[tuple[str, int], ...] = ()
        accelerator = accelerator.strip()
        if accelerator:
            kind, count = accelerator, 1
            head, sep, tail = accelerator.rpartition("x")
            if sep and head and tail.isdigit():
                kind, count = head, int(tail)
            if has_built_in_gpu(machine_type):
                raise ValueError(f"{machine_type} has a built-in GPU; do not add {kind} to it.")
            accelerators = ((kind, count),)
        return cls(machine_type=machine_type, accelerators=accelerators)


def parse_profiles(raw: str) -> tuple[MachineProfile, ...]:
    return tuple(MachineProfile.parse(part) for part in raw.split(",") if part.strip())


@dataclass
class StartOutcome:
    started: bool
    operation: str = ""
    profile: MachineProfile | None = None
    status: str = ""
    # Profiles tried before the one that started, with why each was passed over.
    skipped: list[tuple[str, str]] = field(default_factory=list)
    error: str | None = None


class ReshapeError(RuntimeError):
    pass


def _check(compute: ComputeClient, operation: str, what: str) -> None:
    result = compute.wait_operation(operation)
    if result.failed:
        raise ReshapeError(f"{what} failed: {result.error_message or ', '.join(sorted(result.error_codes))}")
    if not result.done:
        raise ReshapeError(f"{what} did not finish in time.")


def reshape(compute: ComputeClient, instance: InstanceState, profile: MachineProfile) -> None:
    """Turn the stopped VM into ``profile``. Each step waits for the one before."""
    current_attached = tuple(a for a in instance.accelerators if a[1] > 0)
    # An N1 with a T4 must lose the T4 before it can become a G2: a G2 cannot
    # carry an extra GPU. A G2's L4 goes with its machine type, so no step.
    if current_attached and not has_built_in_gpu(instance.machine_type) and current_attached != profile.accelerators:
        _check(compute, compute.set_accelerators(()), "Detaching the GPU")
        current_attached = ()
    if instance.machine_type != profile.machine_type:
        _check(compute, compute.set_machine_type(profile.machine_type), f"Changing the machine type to {profile.machine_type}")
    if profile.accelerators and current_attached != profile.accelerators:
        _check(compute, compute.set_accelerators(profile.accelerators), f"Attaching {profile.label}")


def _wait_until_stopped(compute: ComputeClient, sleep: Callable[[float], None], timeout: float = 90) -> InstanceState:
    deadline = time.monotonic() + timeout
    while True:
        instance = compute.get_instance()
        if instance.status.upper() in _STOPPED or time.monotonic() >= deadline:
            return instance
        sleep(3)


def _unavailable(result: OperationResult) -> bool:
    return bool(result.error_codes & (STOCKOUT_CODES | QUOTA_CODES))


def start_preferred(
    compute: ComputeClient,
    profiles: tuple[MachineProfile, ...],
    *,
    zone: str = "",
    sleep: Callable[[float], None] = time.sleep,
) -> StartOutcome:
    """Start the VM on the first profile Google Cloud can provide.

    With no profiles this is a plain start of the VM as it is. With profiles
    it waits for each start to succeed or fail (failing for want of a GPU is
    quick) so it can fall back while the VM is still stopped. A start that is
    still running when Compute's wait gives up is treated as under way: the
    page goes on watching it.
    """
    instance = compute.get_instance()
    if instance.status.upper() not in _STOPPED:
        return StartOutcome(started=False, status=instance.status)
    if not profiles:
        return StartOutcome(started=True, operation=compute.start_instance(), status=instance.status)

    skipped: list[tuple[str, str]] = []
    last_error: str | None = None
    for index, profile in enumerate(profiles):
        if index:
            instance = _wait_until_stopped(compute, sleep)
            if instance.status.upper() not in _STOPPED:
                return StartOutcome(started=False, status=instance.status, skipped=skipped)
        if not profile.matches(instance):
            try:
                reshape(compute, instance, profile)
            except ReshapeError as error:
                logger.warning("Could not switch the VM to %s: %s", profile.label, error)
                skipped.append((profile.label, str(error)))
                last_error = str(error)
                instance = compute.get_instance()
                continue
            logger.info("VM switched to %s (%s).", profile.machine_type, profile.label)
        operation = compute.start_instance()
        result = compute.wait_operation(operation)
        if not result.failed:
            return StartOutcome(started=True, operation=operation, profile=profile, status=instance.status, skipped=skipped)
        if not _unavailable(result):
            return StartOutcome(started=False, operation=operation, profile=profile, skipped=skipped,
                                error=describe_start_error(result))
        reason = "quota used up" if result.error_codes & QUOTA_CODES else "none free"
        logger.info("No %s available (%s); trying the next profile.", profile.label, reason)
        skipped.append((profile.label, reason))
        last_error = describe_start_error(result)

    tried = ", ".join(label for label, _ in skipped)
    where = f" in {zone}" if zone else ""
    if all(reason == "none free" for _, reason in skipped):
        error = f"Google Cloud has no GPU free{where} right now (tried {tried}). Please try again in a few minutes."
    else:
        error = f"The marker could not be started on any GPU (tried {tried}). {last_error or ''}".strip()
    return StartOutcome(started=False, skipped=skipped, error=error)
