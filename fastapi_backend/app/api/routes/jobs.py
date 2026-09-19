from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.dependencies import get_container, require_expensive_operation, require_job_owner
from app.api.errors import http_error
from app.services.container import AppContainer


router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("")
async def list_jobs(
    sessionId: str | None = Query(None),
    limit: int = Query(200, ge=1, le=200),
    cursor: str | None = Query(None, max_length=512),
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    """A session's own jobs are returned in full (never many). With no
    ``sessionId`` this lists every job in the deployment, so that listing is
    paged the same way ``GET /api/sessions`` is — ``limit``/``cursor`` in,
    ``nextCursor`` back, `None` once there is no more."""
    if sessionId:
        jobs = await container.jobs.list_jobs(sessionId)
        return {"jobs": [container.jobs.public_job(job) for job in jobs], "nextCursor": None}
    try:
        page = await container.jobs.list_jobs_page(limit=limit, cursor=cursor)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "jobs": [container.jobs.public_job(job) for job in page["jobs"]],
        "nextCursor": page["nextCursor"],
    }


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
