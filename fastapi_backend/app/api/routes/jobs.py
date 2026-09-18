from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import get_container, require_expensive_operation, require_job_owner
from app.api.errors import http_error
from app.services.container import AppContainer


router = APIRouter(prefix="/jobs", tags=["jobs"])


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
        raise http_error(error, fallback_message="Failed to load job.", not_found_message="Job not found.") from error


@router.post("/{job_id}/rerun", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_job_owner), Depends(require_expensive_operation)])
async def rerun_job(job_id: str, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    try:
        job = await container.jobs.rerun(job_id)
        return {"job": container.jobs.public_job(job)}
    except Exception as error:
        raise http_error(error, fallback_message="Failed to rerun job.", not_found_message="Job not found.") from error


@router.post("/{job_id}/cancel", dependencies=[Depends(require_job_owner)])
async def cancel_job(job_id: str, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    try:
        job = await container.jobs.cancel(job_id, "Job cancelled by API request.")
        return {"job": container.jobs.public_job(job)}
    except Exception as error:
        raise http_error(error, fallback_message="Failed to cancel job.", not_found_message="Job not found.") from error
