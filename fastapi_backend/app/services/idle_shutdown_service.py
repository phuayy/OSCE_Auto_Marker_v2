"""Switch an on-demand VM off once nobody is using it.

A deployment started by the start gate (``deploy/gcp/gate``) runs on a GPU
machine that bills by the minute, and its users are not expected to remember to
stop it. This loop does it for them: once the deployment has been quiet for
``IDLE_SHUTDOWN_MINUTES``, it runs ``IDLE_SHUTDOWN_COMMAND`` — by default an OS
shutdown, which a GCE instance reports as stopped, so only its disk is billed
until the gate starts it again.

"Quiet" is two things at once:

* no person has acted (``ActivityTracker`` — requests a tab makes on its own
  are not counted, see ``core/activity.py``), and
* no work is in flight: no job queued or running and no upload assembling.
  Work counts as activity, so the window restarts when the last job finishes
  rather than when it started: a three-hour batch of clip assessments queued
  at five o'clock is never cut off, and its results stay up for a full window
  after it ends.

API role only, and only when ``IDLE_SHUTDOWN_MINUTES > 0``.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.activity import ActivityTracker
from app.core.boot_stage import BootStage, BootStageReporter
from app.domain.jobs import IN_PROGRESS_JOB_STATUSES
from app.domain.sessions import SessionStatus


logger = logging.getLogger(__name__)

# The upload assembler owns ``assembling``; queued/processing sessions already
# have a queued or running job row, which the job count covers.
_BUSY_SESSION_STATUSES: frozenset[str] = frozenset({SessionStatus.ASSEMBLING})


class _StatusCounter(Protocol):
    async def count_in_statuses(self, statuses: frozenset[str]) -> int: ...


@dataclass(frozen=True)
class IdleState:
    enabled: bool
    busy: bool
    idle_seconds: float
    threshold_seconds: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "busy": self.busy,
            "idleSeconds": round(self.idle_seconds),
            "thresholdSeconds": self.threshold_seconds,
        }


class IdleShutdownService:
    def __init__(
        self,
        *,
        idle_minutes: int,
        check_interval_seconds: int,
        command: str,
        activity: ActivityTracker,
        jobs: _StatusCounter,
        sessions: _StatusCounter,
        boot_stage: BootStageReporter,
    ) -> None:
        self.threshold_seconds = max(0, idle_minutes) * 60
        self.check_interval_seconds = max(1, check_interval_seconds)
        self.command = command
        self.activity = activity
        self.jobs = jobs
        self.sessions = sessions
        self.boot_stage = boot_stage
        self._shutdown_issued = False

    @property
    def enabled(self) -> bool:
        return self.threshold_seconds > 0

    async def is_busy(self) -> bool:
        if await self.jobs.count_in_statuses(IN_PROGRESS_JOB_STATUSES):
            return True
        return bool(await self.sessions.count_in_statuses(_BUSY_SESSION_STATUSES))

    async def state(self) -> IdleState:
        busy = await self.is_busy()
        return IdleState(
            enabled=self.enabled,
            busy=busy,
            idle_seconds=0.0 if busy else self.activity.idle_seconds(),
            threshold_seconds=self.threshold_seconds,
        )

    async def check_once(self) -> bool:
        """One evaluation. True when this call issued the shutdown."""
        if not self.enabled or self._shutdown_issued:
            return False
        if await self.is_busy():
            self.activity.record()
            return False
        idle = self.activity.idle_seconds()
        if idle < self.threshold_seconds:
            return False
        logger.warning(
            "Idle for %d min with no jobs in flight; shutting down (IDLE_SHUTDOWN_MINUTES=%d).",
            idle // 60,
            self.threshold_seconds // 60,
        )
        await self.boot_stage.report(BootStage.STOPPING)
        if await self._run_command():
            self._shutdown_issued = True
            return True
        return False

    async def loop(self) -> None:
        """Template: ``SessionMaintenanceService.video_retention_sweep_loop`` —
        sleep first, log and continue past a bad check, no ``CancelledError``
        handler so shutdown cancels it cleanly out of ``asyncio.sleep``."""
        while True:
            await asyncio.sleep(self.check_interval_seconds)
            try:
                await self.check_once()
            except Exception:
                logger.exception("Idle-shutdown check failed; will retry on the next interval.")

    async def _run_command(self) -> bool:
        argv = shlex.split(self.command)
        if not argv:
            logger.error("IDLE_SHUTDOWN_COMMAND is empty; nothing to run.")
            return False
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _stdout, stderr = await process.communicate()
        except OSError as error:
            logger.error("Could not run IDLE_SHUTDOWN_COMMAND %r: %s", self.command, error)
            return False
        if process.returncode != 0:
            # Left un-issued, so the next interval tries again: a sudoers rule
            # fixed by hand should not need a restart to take effect.
            logger.error(
                "IDLE_SHUTDOWN_COMMAND exited %s: %s",
                process.returncode,
                stderr.decode(errors="replace").strip()[:500],
            )
            return False
        return True
