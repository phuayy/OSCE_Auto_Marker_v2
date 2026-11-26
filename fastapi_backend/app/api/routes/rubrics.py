from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse

from app.api.dependencies import current_actor, get_container, require_admin
from app.domain.communication_rubric import RubricValidationError
from app.schemas.communication_rubric import CommunicationRubricSaveRequest
from app.services.container import AppContainer
from app.services.rubric_service import (
    RubricDraftNotFound,
    RubricParseError,
    RubricVersionAlreadyActive,
    RubricVersionConflict,
    RubricVersionNotFound,
)

DRAFT_GONE = "That extraction has expired or was discarded. Upload the PDF again."

# The communication rubric every score is computed against is deployment-wide
# (see CLAUDE.md "Two-tier settings") — every marker reads it, only an admin
# replaces it.
router = APIRouter(prefix="/communication-rubric", tags=["communication-rubric"])
admin_router = APIRouter(
    prefix="/admin/communication-rubric",
    tags=["communication-rubric"],
    dependencies=[Depends(require_admin)],
)


@router.get("")
async def get_communication_rubric(container: AppContainer = Depends(get_container)) -> dict[str, object]:
    return await container.rubrics.current()


@router.get("/pdf")
async def get_communication_rubric_pdf(container: AppContainer = Depends(get_container)) -> FileResponse:
    meta = await container.rubrics.read_pdf_meta()
    if not meta:
        raise HTTPException(status_code=404, detail="No communication rubric PDF is available.")
    absolute_path = str(meta["absolutePath"])
    return FileResponse(
        absolute_path,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{meta["fileName"]}"'},
    )


@router.get("/versions")
async def list_communication_rubric_versions(
    limit: int = Query(50, ge=1, le=200),
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    """Every version, newest first, without documents. Read by every marker:
    which rubric a score was computed against is not an admin secret."""
    await container.rubrics.ensure_parsed()  # an upgraded deployment's rubric is adopted as v1
    return {"versions": await container.rubrics.list_versions(limit=limit)}


@router.get("/versions/{version_id}")
async def get_communication_rubric_version(
    version_id: str, container: AppContainer = Depends(get_container)
) -> dict[str, object]:
    try:
        return {"version": await container.rubrics.get_version(version_id)}
    except RubricVersionNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@admin_router.post("/versions/{version_id}/restore")
async def restore_communication_rubric_version(
    version_id: str, request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, object]:
    try:
        return await container.rubrics.restore_version(version_id, actor=current_actor(request))
    except RubricVersionNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RubricVersionAlreadyActive as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@admin_router.post("")
async def upload_communication_rubric(
    request: Request,
    rubric: UploadFile | None = File(None),
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    if rubric is None:
        raise HTTPException(status_code=400, detail='Rubric PDF file is required (field name "rubric").')
    try:
        return await container.rubrics.upload(rubric, actor=current_actor(request))
    except RubricParseError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@admin_router.post("/reset")
async def reset_communication_rubric(
    request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, object]:
    try:
        return await container.rubrics.reset(actor=current_actor(request))
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RubricParseError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@admin_router.post("/drafts", status_code=201)
async def create_communication_rubric_draft(
    request: Request,
    mode: str = "llm",
    rubric: UploadFile | None = File(None),
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    if rubric is None:
        raise HTTPException(status_code=400, detail='Rubric PDF file is required (field name "rubric").')
    try:
        return await container.rubrics.extract_draft(rubric, mode=mode, actor=current_actor(request))
    except RubricParseError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@admin_router.delete("/drafts/{draft_id}", status_code=204)
async def discard_communication_rubric_draft(
    draft_id: str, container: AppContainer = Depends(get_container)
) -> Response:
    try:
        await container.rubrics.discard_draft(draft_id)
    except RubricDraftNotFound as error:
        raise HTTPException(status_code=404, detail=DRAFT_GONE) from error
    return Response(status_code=204)


@admin_router.put("")
async def save_communication_rubric(
    body: CommunicationRubricSaveRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    try:
        return await container.rubrics.save_edited(
            body.rubric, base_version_id=body.baseVersionId, draft_id=body.draftId, actor=current_actor(request)
        )
    except RubricValidationError as error:
        raise HTTPException(
            status_code=422,
            detail={"message": "The rubric has problems to fix before it can be saved.", "issues": error.issues},
        ) from error
    except RubricVersionConflict as error:
        raise HTTPException(
            status_code=409, detail={"message": str(error), "currentVersion": error.current}
        ) from error
    except RubricDraftNotFound as error:
        raise HTTPException(status_code=404, detail=DRAFT_GONE) from error
    except RubricParseError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
