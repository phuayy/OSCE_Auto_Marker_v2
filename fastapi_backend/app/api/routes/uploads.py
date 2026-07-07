from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.api.dependencies import get_container
from app.core.exceptions import AppError
from app.services.clip_service import ClipService
from app.services.container import AppContainer


router = APIRouter(tags=["uploads"])


@router.post("/upload")
async def upload_session(
    video: UploadFile | None = File(None),
    caseStudy: UploadFile | None = File(None),
    sessionName: str | None = Form(None),
    segmentation: str | None = Form(None),
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    try:
        container.artifacts.validate_video_upload(video)
        container.artifacts.validate_pdf_upload(caseStudy, field_name="caseStudy")
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    if video is None:
        raise HTTPException(status_code=400, detail="A video file is required.")
    if caseStudy is None:
        raise HTTPException(status_code=400, detail="A case study PDF file is required.")
    session_id = container.artifacts.new_session_id()
    try:
        entries, used_keys = await container.sessions.ensure_names_for_index(
            await container.sessions.read_all_entries()
        )
        _ = entries
        video_meta = await container.storage.save_uploaded_source(
            video,
            session_id=session_id,
            kind="video",
            max_bytes=container.settings.max_video_upload_bytes,
        )
        case_study_meta = await container.storage.save_uploaded_source(
            caseStudy,
            session_id=session_id,
            kind="caseStudy",
            max_bytes=container.settings.max_case_study_upload_bytes,
        )
        case_study_meta = await container.rubric_assets.register_case_study_meta(case_study_meta)
        # Auto-crop segmentation method for the legacy multipart path; unknown
        # values are dropped so a stale client cannot poison the session.
        segmentation_method = str(segmentation or "").strip().lower()
        if segmentation_method == "human":
            segmentation_method = "person"
        if segmentation_method not in {"bells", "person"}:
            segmentation_method = None
        session = {
            "id": session_id,
            "name": container.sessions.reserve_unique_session_name(used_keys, (sessionName or "").strip()),
            "createdAt": container.pipeline.now_iso(),
            "status": "uploaded",
            "segmentation": segmentation_method,
            "pipeline": {
                "startedAt": None,
                "endedAt": None,
                "runtimeSeconds": None,
                "mode": container.settings.whisperx_device,
            },
            "files": {"video": video_meta, "caseStudy": case_study_meta},
            "outputs": ClipService.empty_outputs(),
            "error": None,
        }
        await container.sessions.write(session)
        return {"session": container.sessions.public_session(session)}
    except AppError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error) or "Upload failed.") from error
