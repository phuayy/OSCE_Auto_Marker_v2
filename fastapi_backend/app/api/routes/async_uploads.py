from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.dependencies import get_container
from app.core.exceptions import AppError
from app.schemas.uploads import CompleteUploadRequest, InitiateUploadRequest
from app.services.container import AppContainer


router = APIRouter(prefix="/uploads", tags=["async uploads"])


def _http_error(error: Exception, fallback_message: str = "Upload request failed.") -> HTTPException:
    if isinstance(error, AppError):
        return HTTPException(status_code=error.status_code, detail=error.message)
    if isinstance(error, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(error) or "Upload not found.")
    if isinstance(error, ValueError):
        return HTTPException(status_code=400, detail=str(error))
    return HTTPException(status_code=500, detail=str(error) or fallback_message)


@router.post("/initiate", status_code=status.HTTP_201_CREATED)
async def initiate_upload(
    payload: InitiateUploadRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    try:
        return await container.async_uploads.initiate(payload)
    except Exception as error:
        raise _http_error(error, "Upload initiation failed.") from error


@router.put("/{upload_id}/parts/{part_number}")
async def put_upload_part(
    upload_id: str,
    part_number: int,
    request: Request,
    fileId: str | None = Query(None),
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    try:
        body = await request.body()
        if not body:
            raise AppError("Upload part body is required.", status_code=400)
        return await container.async_uploads.put_part(upload_id, fileId, part_number, body)
    except Exception as error:
        raise _http_error(error, "Upload part failed.") from error


@router.get("/{upload_id}")
async def get_upload_status(upload_id: str, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    try:
        return await container.async_uploads.status(upload_id)
    except Exception as error:
        raise _http_error(error, "Failed to load upload status.") from error


@router.post("/{upload_id}/complete", status_code=status.HTTP_202_ACCEPTED)
async def complete_upload(
    upload_id: str,
    payload: CompleteUploadRequest | None = None,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    try:
        return await container.async_uploads.complete(upload_id, payload or CompleteUploadRequest())
    except Exception as error:
        raise _http_error(error, "Upload finalization failed.") from error


@router.delete("/{upload_id}")
async def abort_upload(upload_id: str, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    try:
        return await container.async_uploads.abort(upload_id)
    except Exception as error:
        raise _http_error(error, "Upload abort failed.") from error
