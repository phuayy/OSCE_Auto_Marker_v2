from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.api.dependencies import get_container
from app.services.container import AppContainer


router = APIRouter(prefix="/communication-rubric", tags=["communication-rubric"])


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


@router.post("")
async def upload_communication_rubric(
    rubric: UploadFile | None = File(None),
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    if rubric is None:
        raise HTTPException(status_code=400, detail='Rubric PDF file is required (field name "rubric").')
    try:
        return await container.rubrics.upload(rubric)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@router.post("/reset")
async def reset_communication_rubric(container: AppContainer = Depends(get_container)) -> dict[str, object]:
    try:
        return await container.rubrics.reset()
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
