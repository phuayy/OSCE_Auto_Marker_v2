from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query, Request, status

from app.api.dependencies import get_container
from app.api.errors import http_error
from app.core.exceptions import AppError
from app.schemas.uploads import CompleteUploadRequest, InitiateUploadRequest
from app.services.container import AppContainer


router = APIRouter(prefix="/uploads", tags=["async uploads"])


@router.post("/initiate", status_code=status.HTTP_201_CREATED)
async def initiate_upload(
    payload: InitiateUploadRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    try:
        return await container.async_uploads.initiate(payload)
    except Exception as error:
        raise http_error(error, fallback_message="Upload initiation failed.", not_found_message="Upload not found.") from error


@router.put("/{upload_id}/parts/{part_number}")
async def put_upload_part(
    upload_id: str,
    request: Request,
    part_number: int = Path(ge=1, le=100_000),
    fileId: str | None = Query(None),
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    try:
        # Reject on the declared length *before* reading. `request.body()`
        # buffers the whole payload in memory, so checking the size only after
        # it has been read is no protection at all — a single oversized PUT
        # would exhaust the process before reaching the check.
        _reject_oversized_part(request, container.settings.upload_part_size_bytes)
        body = await request.body()
        if not body:
            raise AppError("Upload part body is required.", status_code=400)
        # A client may lie about (or omit) Content-Length; the authoritative
        # check is on the bytes actually received, inside put_part.
        return await container.async_uploads.put_part(upload_id, fileId, part_number, body)
    except Exception as error:
        raise http_error(error, fallback_message="Upload part failed.", not_found_message="Upload not found.") from error


def _reject_oversized_part(request: Request, max_bytes: int) -> None:
    raw_length = request.headers.get("content-length")
    if raw_length is None:
        return
    try:
        declared = int(raw_length)
    except (TypeError, ValueError):
        raise AppError("Content-Length must be an integer.", status_code=400) from None
    if declared > max_bytes:
        raise AppError(
            f"Upload part exceeds the {max_bytes // (1024 * 1024)} MB part size limit.",
            status_code=413,
        )


@router.get("/{upload_id}")
async def get_upload_status(upload_id: str, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    try:
        return await container.async_uploads.status(upload_id)
    except Exception as error:
        raise http_error(error, fallback_message="Failed to load upload status.", not_found_message="Upload not found.") from error


@router.post("/{upload_id}/complete", status_code=status.HTTP_202_ACCEPTED)
async def complete_upload(
    upload_id: str,
    payload: CompleteUploadRequest | None = None,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    try:
        return await container.async_uploads.complete(upload_id, payload or CompleteUploadRequest())
    except Exception as error:
        raise http_error(error, fallback_message="Upload finalization failed.", not_found_message="Upload not found.") from error


@router.delete("/{upload_id}")
async def abort_upload(upload_id: str, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    try:
        return await container.async_uploads.abort(upload_id)
    except Exception as error:
        raise http_error(error, fallback_message="Upload abort failed.", not_found_message="Upload not found.") from error
