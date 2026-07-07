from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.dependencies import get_container
from app.core.exceptions import AppError
from app.services.container import AppContainer


router = APIRouter(prefix="/jobs", tags=["jobs"])


def _http_error(error: Exception, fallback_message: str = "Job request failed.") -> HTTPException:
    if isinstance(error, AppError):
        return HTTPException(status_code=error.status_code, detail=error.message)
    if isinstance(error, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(error) or "Job not found.")
    if isinstance(error, ValueError):
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=500, detail=str(error) or fallback_message)


@router.get("")
async def list_jobs(
    sessionId: str | None = Query(None),
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    jobs = await container.jobs.list_jobs(sessionId)
    return {"jobs": [container.jobs.public_job(job) for job in jobs]}


@router.get("/{job_id}")
async def get_job(job_id: str, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    try:
        job = await container.jobs.read(job_id)
        return {"job": container.jobs.public_job(job)}
    except Exception as error:
        raise _http_error(error, "Failed to load job.") from error


@router.post("/{job_id}/rerun", status_code=status.HTTP_202_ACCEPTED)
async def rerun_job(job_id: str, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    try:
        job = await container.jobs.rerun(job_id)
        return {"job": container.jobs.public_job(job)}
    except Exception as error:
        raise _http_error(error, "Failed to rerun job.") from error


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    try:
        job = await container.jobs.cancel(job_id, "Job cancelled by API request.")
        return {"job": container.jobs.public_job(job)}
    except Exception as error:
        raise _http_error(error, "Failed to cancel job.") from error
