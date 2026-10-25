"""What the person on the start page is told: one pure function from the
VM's state to a stage.

Three sources, each covering the part of a boot the others cannot see:

* the Compute API's instance ``status`` — off, being provisioned, running,
  stopping. Covers everything before the operating system is up;
* the ``osce/boot`` guest attribute the app writes at each startup step
  (``fastapi_backend/app/core/boot_stage.py``) — covers the app's own boot,
  while it cannot answer HTTP yet;
* the app's readiness probe (``/api/health/ready``) — the only thing that
  says "ready". A guest attribute saying so is not enough: the page must not
  send anyone to an address that does not answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Any

# The happy path, in order. The page draws these as a checklist.
STEPS: tuple[tuple[str, str], ...] = (
    ("requested", "Start requested"),
    ("provisioning", "Reserving the GPU machine"),
    ("booting", "Starting the operating system"),
    ("app-starting", "Starting the marker"),
    ("database", "Preparing the database"),
    ("loading", "Loading settings and rubrics"),
    ("ready", "Ready"),
)
_STEP_KEYS = tuple(key for key, _ in STEPS)

# Off the happy path: shown on their own, without the checklist.
OFF_PATH: dict[str, str] = {
    "stopped": "Not running",
    "stopping": "Shutting down. You can start it again once it has stopped.",
    "unavailable": "The machine is under maintenance by Google Cloud. Try again in a few minutes.",
    "failed": "The machine could not be started.",
}

_STOPPED = {"TERMINATED", "STOPPED", "SUSPENDED"}
_STOPPING = {"STOPPING", "SUSPENDING"}
_PROVISIONING = {"PROVISIONING", "STAGING", "PENDING"}
# Guest-attribute stages the app writes, mapped onto STEPS. "ready" from the
# app without a passing probe means "almost": the listener is coming up.
_BOOT_TO_STEP = {
    "app-starting": "app-starting",
    "database": "database",
    "loading": "loading",
    "ready": "loading",
    "stopping": "stopping",
}
# A guest attribute is kept across a stop/start, so a value written before
# this run's start time is the previous run's and must be ignored. A little
# slack for the two clocks involved.
_CLOCK_SLACK_SECONDS = 30


@dataclass(frozen=True)
class BootValue:
    stage: str
    at: float


def parse_boot_value(raw: str | None) -> BootValue | None:
    if not raw:
        return None
    parts = raw.strip().split()
    if len(parts) != 2:
        return None
    try:
        return BootValue(stage=parts[0], at=float(parts[1]))
    except ValueError:
        return None


def parse_timestamp(raw: str | None) -> float | None:
    """Compute API RFC 3339 timestamps (``2026-10-25T10:00:00.123-07:00``)."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass(frozen=True)
class Observation:
    instance_status: str
    last_start: float | None
    boot: BootValue | None
    app_ready: bool
    start_requested: bool = False
    start_error: str | None = None


def derive_stage(observation: Observation) -> str:
    status = observation.instance_status.upper()
    if observation.start_error:
        return "failed"
    if status in _STOPPED:
        return "requested" if observation.start_requested else "stopped"
    if status in _STOPPING:
        return "stopping"
    if status in _PROVISIONING:
        return "provisioning"
    if status != "RUNNING":
        return "unavailable"
    if observation.app_ready:
        return "ready"
    boot = observation.boot
    if boot is not None and (
        observation.last_start is None or boot.at >= observation.last_start - _CLOCK_SLACK_SECONDS
    ):
        return _BOOT_TO_STEP.get(boot.stage, "booting")
    return "booting"


def label_for(stage: str) -> str:
    return dict(STEPS).get(stage) or OFF_PATH.get(stage, stage)


def checklist(stage: str) -> list[dict[str, str]]:
    """The happy path with each step done, current or pending. Empty off it."""
    if stage not in _STEP_KEYS:
        return []
    current = _STEP_KEYS.index(stage)
    return [
        {
            "key": key,
            "label": label,
            "state": "done" if index < current or stage == "ready" else "current" if index == current else "pending",
        }
        for index, (key, label) in enumerate(STEPS)
    ]


def estimate_seconds(history: list[float], default_seconds: int) -> int:
    """How long a start usually takes: the median of recent ones, else the default."""
    usable = [value for value in history if 0 < value < 3 * 3600]
    return round(median(usable)) if usable else default_seconds


def describe(
    observation: Observation,
    *,
    now: float,
    history: list[float],
    default_estimate_seconds: int,
) -> dict[str, Any]:
    stage = derive_stage(observation)
    starting = stage in _STEP_KEYS and stage not in {"requested", "ready"}
    elapsed = (
        max(0, round(now - observation.last_start))
        if starting and observation.last_start is not None
        else None
    )
    estimate = estimate_seconds(history, default_estimate_seconds)
    return {
        "stage": stage,
        "label": label_for(stage),
        "running": stage == "ready",
        "starting": stage in _STEP_KEYS and stage != "ready",
        "steps": checklist(stage),
        "elapsedSeconds": elapsed,
        "estimateSeconds": estimate,
        "error": observation.start_error,
    }
