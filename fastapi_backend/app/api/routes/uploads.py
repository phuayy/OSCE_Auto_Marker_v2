from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import ValidationError

from app.api.dependencies import get_container
from app.core.exceptions import AppError
from app.domain.sessions import SessionStatus, empty_outputs
from app.schemas.uploads import LegacyUploadForm
from app.services.container import AppContainer

router = APIRouter(tags=["uploads"])


@router.post("/upload")
async def upload_session(
    video: UploadFile | None = File(None),
    caseStudy: UploadFile | None = File(None),
    sessionName: str | None = Form(None),
    segmentation: str | None = Form(None),
    # JSON object string — multipart cannot carry a nested object any other way.
    segmentationOptions: str | None = Form(None),
    workflow: str | None = Form(None),
    corpusId: str | None = Form(None),
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    try:
        # Route the loose multipart form fields through the same validated
        # schema the async initiate path uses, so both entry points enforce
        # identical invariants (trimmed/bounded name, known workflow, known
        # segmentation, segmentation only meaningful for long uploads).
        form = LegacyUploadForm(
            sessionName=sessionName,
            segmentation=segmentation,
            segmentationOptions=segmentationOptions,
            workflow=workflow,
            corpusId=corpusId,
        )
        container.artifacts.validate_video_upload(video)
        container.artifacts.validate_pdf_upload(caseStudy, field_name="caseStudy")
    except ValidationError as error:
        first = (error.errors() or [{}])[0]
        raise HTTPException(status_code=400, detail=str(first.get("msg") or "Invalid upload form.")) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    try:
        corpus_snapshot = await container.corpora.snapshot(form.corpusId)
    except LookupError as error:
        raise HTTPException(status_code=400, detail="Transcription corpus not found.") from error

    if video is None:
        raise HTTPException(status_code=400, detail="A video file is required.")
    if caseStudy is None:
        raise HTTPException(status_code=400, detail="A case study PDF file is required.")
    session_id = container.artifacts.new_session_id()
    try:
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
        session = {
            "id": session_id,
            "name": form.sessionName or "",
            "createdAt": container.pipeline.now_iso(),
            "status": SessionStatus.UPLOADED,
            # Persisting the workflow is what lets the frontend render a long
            # session with the clip workflow (and gate it while cropping) even
            # on this legacy path — deriving it from detected clips alone
            # leaves a window where the session looks like a standard one.
            "workflow": form.workflow,
            "segmentation": form.segmentation,
            "segmentationOptions": form.resolved_segmentation_options(),
            "corpus": corpus_snapshot,
            "pipeline": {
                "startedAt": None,
                "endedAt": None,
                "runtimeSeconds": None,
                "mode": container.settings.whisperx_device,
            },
            "files": {"video": video_meta, "caseStudy": case_study_meta},
            "outputs": empty_outputs(),
            "error": None,
        }
        await container.sessions.create_named(session)
        return {"session": container.sessions.public_session(session)}
    except AppError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error) or "Upload failed.") from error
