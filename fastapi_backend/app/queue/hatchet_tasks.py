from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

from app.core.config import Settings

if TYPE_CHECKING:  # pragma: no cover - import cycle guard (container imports the queue)
    from app.services.container import AppContainer


T = TypeVar("T")
settings = Settings.load()

# The worker process's container. The worker lifespan binds it once; every job
# that lands in this process then runs against the same connections, caches and
# GPU lease instead of booting a fresh container (with the API's startup sweeps)
# per job. Guarded by a lock because the fallback below may build one lazily
# when a job arrives in a process that never ran the lifespan.
_worker_container: "AppContainer | None" = None
_worker_container_lock: asyncio.Lock | None = None


class ProcessJobInput(BaseModel):
    job_id: str


class MissingHatchetTask:
    def __init__(self, name: str, reason: Exception | str) -> None:
        self.name = name
        self.reason = reason

    async def aio_run(self, *_args: Any, **_kwargs: Any) -> Any:
        message = (
            "Hatchet queue backend is not available. Install hatchet-sdk, configure "
            "HATCHET_CLIENT_TOKEN, and run a Hatchet control plane, or set JOB_QUEUE_BACKEND=local."
        )
        if isinstance(self.reason, Exception):
            raise RuntimeError(message) from self.reason
        raise RuntimeError(f"{message} Reason: {self.reason}")


class MissingHatchetClient:
    def __init__(self, reason: Exception | str) -> None:
        self.reason = reason

    def task(self, function: T | None = None, **kwargs: Any) -> Any:
        def _decorate(inner: T) -> MissingHatchetTask:
            return MissingHatchetTask(str(kwargs.get("name") or getattr(inner, "__name__", "hatchet-task")), self.reason)

        return _decorate(function) if function is not None else _decorate

    def worker(self, *_args: Any, **_kwargs: Any) -> Any:
        message = (
            "Hatchet worker cannot start because hatchet-sdk is unavailable or misconfigured. "
            "Install dependencies and set HATCHET_CLIENT_TOKEN."
        )
        if isinstance(self.reason, Exception):
            raise RuntimeError(message) from self.reason
        raise RuntimeError(f"{message} Reason: {self.reason}")


def create_hatchet_client() -> Any:
    try:
        from hatchet_sdk import Hatchet
    except Exception as error:
        return MissingHatchetClient(error)
    try:
        return Hatchet()
    except Exception as error:
        return MissingHatchetClient(error)


def _retry_count(ctx: Any) -> int:
    value = getattr(ctx, "retry_count", 0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _is_cancelled(ctx: Any) -> bool:
    return bool(getattr(ctx, "exit_flag", False) or getattr(ctx, "cancelled", False))


hatchet = create_hatchet_client()


def bind_worker_container(container: "AppContainer | None") -> None:
    """Hand the worker's container to the task; ``None`` unbinds it at shutdown."""
    global _worker_container
    _worker_container = container


async def get_worker_container() -> "AppContainer":
    """The process-wide worker container, built on first use if nobody bound one."""
    global _worker_container, _worker_container_lock
    if _worker_container is not None:
        return _worker_container
    if _worker_container_lock is None:
        _worker_container_lock = asyncio.Lock()
    async with _worker_container_lock:
        if _worker_container is None:
            from app.services.container import ContainerRole, create_container

            container = create_container(settings)
            await container.startup(role=ContainerRole.WORKER)
            _worker_container = container
    return _worker_container


async def shutdown_worker_container() -> None:
    """Tear down a lazily built container (the lifespan owns the bound one)."""
    global _worker_container
    container, _worker_container = _worker_container, None
    if container is not None:
        with contextlib.suppress(Exception):
            await container.shutdown()


@hatchet.task(
    name="osce-process-job",
    input_validator=ProcessJobInput,
    retries=max(0, settings.hatchet_job_retries),
    schedule_timeout=timedelta(minutes=max(1, settings.hatchet_job_schedule_timeout_minutes)),
    execution_timeout=timedelta(minutes=max(1, settings.hatchet_job_execution_timeout_minutes)),
)
async def process_job(input: ProcessJobInput, ctx: Any) -> dict[str, str]:
    if _is_cancelled(ctx):
        raise RuntimeError("Hatchet task was cancelled before job execution started.")

    container = await get_worker_container()
    await container.jobs.prepare_hatchet_retry_attempt(input.job_id, _retry_count(ctx))
    await container.jobs.run_job(input.job_id, raise_on_error=True)
    return {"jobId": input.job_id, "status": "completed"}


async def enqueue_process_job(job_id: str) -> Any:
    return await process_job.aio_run_no_wait(input=ProcessJobInput(job_id=job_id))
