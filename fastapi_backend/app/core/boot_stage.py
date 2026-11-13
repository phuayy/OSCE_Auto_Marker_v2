"""Tell the start gate how far this process has got, while HTTP cannot.

uvicorn serves nothing until the FastAPI lifespan has finished, and startup
(migrations, seed data, the rubric parse) can take a while on a VM that has
just been switched on. The start gate (``deploy/gcp/gate``) shows the person
waiting which stage the boot is at, so each stage is published where the gate
can read it without the app answering: a GCE guest attribute, written to the
instance's own metadata server and read back through the Compute API.

One key, ``osce/boot``, holds ``"<stage> <unix seconds>"``. One key rather than
two so the stage and its time can never be read half-updated; the time lets the
gate ignore a value left over from the VM's previous run.

Best effort throughout: a failed write is logged and the boot carries on. The
gate falls back to the VM's own status and the readiness probe, so the worst a
failure costs is a less specific label on the loading screen.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from enum import StrEnum

import httpx


logger = logging.getLogger(__name__)

GUEST_ATTRIBUTE_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/guest-attributes/osce/boot"
)


class BootStage(StrEnum):
    # Spelled as the gate reads them (deploy/gcp/gate/gate/stages.py).
    STARTING = "app-starting"
    DATABASE = "database"
    LOADING = "loading"
    READY = "ready"
    STOPPING = "stopping"


def encode_boot_value(stage: BootStage, at: float) -> str:
    return f"{stage.value} {int(at)}"


class BootStageReporter:
    def __init__(
        self,
        enabled: bool,
        *,
        url: str = GUEST_ATTRIBUTE_URL,
        timeout_seconds: float = 2.0,
        clock: Callable[[], float] = time.time,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.enabled = enabled
        self._url = url
        self._timeout = timeout_seconds
        self._clock = clock
        self._transport = transport
        self._warned = False

    async def report(self, stage: BootStage) -> None:
        if not self.enabled:
            return
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                response = await client.put(
                    self._url,
                    content=encode_boot_value(stage, self._clock()),
                    headers={"Metadata-Flavor": "Google"},
                )
                response.raise_for_status()
        except Exception as error:
            # Once, not per stage: a VM without guest attributes enabled would
            # otherwise log the same refusal at every step of every boot.
            if not self._warned:
                self._warned = True
                logger.warning(
                    "Could not publish boot stage %s as a guest attribute (%s). "
                    "Is enable-guest-attributes set on the instance?",
                    stage.value,
                    error,
                )
