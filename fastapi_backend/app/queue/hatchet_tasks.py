from __future__ import annotations

from datetime import timedelta
from typing import Any, TypeVar

from pydantic import BaseModel

from app.core.config import Settings


T = TypeVar("T")
settings = Settings.load()


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

    from app.services.container import create_container

    container = create_container(settings)
    await container.startup(dispatch_queued_jobs=False, recover_interrupted_jobs=False)
    try:
        await container.jobs.prepare_hatchet_retry_attempt(input.job_id, _retry_count(ctx))
        await container.jobs.run_job(input.job_id, raise_on_error=True)
        return {"jobId": input.job_id, "status": "completed"}
    finally:
        await container.shutdown()


async def enqueue_process_job(job_id: str) -> Any:
    return await process_job.aio_run_no_wait(input=ProcessJobInput(job_id=job_id))
