from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.api.dependencies import get_container
from app.core.exceptions import AppError
from app.schemas.sessions import ManualClipsRequest, RecropClipRequest, RenameClipRequest, RenameSessionRequest
from app.services.container import AppContainer


router = APIRouter(prefix="/sessions", tags=["sessions"])


def _http_error(error: Exception, fallback_status: int = 500, fallback_message: str = "Unexpected server error.") -> HTTPException:
    if isinstance(error, AppError):
        return HTTPException(status_code=error.status_code, detail=error.message)
    if isinstance(error, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(error) or "Session not found.")
    if isinstance(error, ValueError):
        return HTTPException(status_code=400, detail=str(error))
    return HTTPException(status_code=fallback_status, detail=str(error) or fallback_message)


@router.get("")
async def list_sessions(container: AppContainer = Depends(get_container)) -> dict[str, object]:
    return {"sessions": await container.sessions.list_sessions()}


@router.patch("/{session_id}/name")
async def rename_session(
    session_id: str,
    payload: RenameSessionRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    try:
        session = await container.sessions.rename_session(session_id, payload.name)
        return {"session": container.sessions.public_session(session)}
    except FileExistsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception as error:
        raise _http_error(error, fallback_message="Failed to rename session.") from error


@router.get("/{session_id}")
async def get_session(session_id: str, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    try:
        session = await container.sessions.read(session_id)
        session = await container.sessions.ensure_session_name(session_id, session)
        # Generate the browser subtitle track (VTT file + in-memory metadata) for
        # the response, but do NOT persist from this read path: the worker may be
        # mid-processing and a write from a stale read here would clobber its
        # concurrent status/pipeline updates (lost update). The VTT file is written
        # to disk by ensure_session_subtitle_track and is regenerated cheaply.
        await container.media.ensure_session_subtitle_track(session)
        return {"session": container.sessions.public_session(session)}
    except Exception as error:
        raise HTTPException(status_code=404, detail="Session not found.") from error


@router.get("/{session_id}/transcript")
async def get_transcript(session_id: str, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    try:
        session = await container.sessions.read(session_id)
        # Read path: generate the subtitle track for the response without
        # persisting, to avoid clobbering concurrent worker writes (see
        # get_session above).
        await container.media.ensure_session_subtitle_track(session)
        transcript = await container.sessions.read_output_payload(session, "transcript")
        return {"session": container.sessions.public_session(session), "transcript": transcript}
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error) or "Session or transcript not found.") from error


@router.get("/{session_id}/scores")
async def get_scores(session_id: str, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    try:
        session = await container.sessions.read(session_id)
        scores = await container.sessions.read_output_payload(session, "scores")
        return {"session": container.sessions.public_session(session), "scores": scores}
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error) or "Session or scores not found.") from error


@router.get("/{session_id}/audio-professionalism")
async def get_audio_professionalism(session_id: str, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    try:
        session = await container.sessions.read(session_id)
        payload = await container.sessions.read_output_payload(session, "audioProfessionalism")
        return {"session": container.sessions.public_session(session), "audioProfessionalism": payload}
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error) or "Session or audio professionalism output not found.") from error


@router.get("/{session_id}/communication-scores")
async def get_communication_scores(session_id: str, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    try:
        session = await container.sessions.read(session_id)
        payload = await container.sessions.read_output_payload(session, "communicationScores")
        return {"session": container.sessions.public_session(session), "communicationScores": payload}
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error) or "Session or communication scores not found.") from error


@router.get("/{session_id}/clip-summaries")
async def get_clip_summaries(session_id: str, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    try:
        return await container.clips.clip_summaries(session_id)
    except Exception as error:
        raise _http_error(error, fallback_message="Failed to aggregate clip summaries.") from error


@router.get("/{session_id}/events")
async def session_events(session_id: str, container: AppContainer = Depends(get_container)) -> StreamingResponse:
    stream = await container.events.connect(session_id)
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{session_id}/auto-crop")
async def auto_crop_session(session_id: str, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    try:
        return await container.clips.auto_crop_session_by_id(session_id)
    except Exception as error:
        raise _http_error(error, fallback_message="Auto-crop failed.") from error


@router.post("/{session_id}/process")
async def process_session(session_id: str, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    try:
        return await container.pipeline.process_session_by_id(session_id)
    except AppError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    except Exception as error:
        await container.pipeline.mark_session_failed(session_id, error)
        raise HTTPException(status_code=500, detail=str(error) or "Processing failed.") from error


@router.post("/{session_id}/clips/manual")
async def create_manual_clips(
    session_id: str,
    payload: ManualClipsRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    try:
        return await container.clips.manual_clips(session_id, payload.boundaries, payload.labels)
    except Exception as error:
        raise _http_error(error, fallback_message="Manual clip split failed.") from error


@router.post("/{session_id}/clips/{clip_id}/recrop")
async def recrop_clip(
    session_id: str,
    clip_id: str,
    payload: RecropClipRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    try:
        return await container.clips.recrop_clip(session_id, clip_id, payload.start, payload.end)
    except Exception as error:
        raise _http_error(error, fallback_message="Recrop failed.") from error


@router.post("/{session_id}/clips/{clip_id}/assess")
async def assess_clip(
    session_id: str,
    clip_id: str,
    defer: str | None = Query(None),
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    defer_enabled = str(defer or "").lower() in {"1", "true", "yes"}
    try:
        return await container.clips.assess_clip(session_id, clip_id, defer_enabled)
    except Exception as error:
        raise _http_error(error, fallback_message="Clip assessment failed.") from error


@router.patch("/{session_id}/clips/{clip_id}")
async def rename_clip(
    session_id: str,
    clip_id: str,
    payload: RenameClipRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    try:
        return await container.clips.rename_clip(session_id, clip_id, payload.label)
    except Exception as error:
        raise _http_error(error, fallback_message="Failed to rename clip.") from error
