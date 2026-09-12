from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.dependencies import get_container
from app.api.errors import http_error
from app.schemas.sessions import (
    ManualClipsRequest,
    RecropClipRequest,
    RenameClipRequest,
    RenameSessionRequest,
)
from app.services.container import AppContainer

router = APIRouter(prefix="/sessions", tags=["sessions"])


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
        raise http_error(error, fallback_message="Failed to rename session.", not_found_message="Session not found.") from error


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
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found.") from error
    except Exception as error:
        # A subtitle-generation or serialization failure is NOT a missing
        # session — masking it as 404 sends the client down the wrong path.
        raise http_error(error, fallback_message="Failed to load session.", not_found_message="Session not found.") from error


@router.delete("/{session_id}")
async def delete_session(session_id: str, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    try:
        return await container.session_maintenance.delete_session(session_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found.") from error
    except Exception as error:
        raise http_error(error, fallback_message="Failed to delete session.", not_found_message="Session not found.") from error


@router.post("/{session_id}/rerun")
async def rerun_session(session_id: str, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    try:
        return await container.session_maintenance.rerun_session(session_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found.") from error
    except Exception as error:
        raise http_error(error, fallback_message="Failed to re-run session.", not_found_message="Session not found.") from error


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
        raise http_error(error, fallback_message="Failed to aggregate clip summaries.", not_found_message="Session not found.") from error


@router.get("/{session_id}/events")
async def session_events(session_id: str, container: AppContainer = Depends(get_container)) -> StreamingResponse:
    if not container.settings.session_sse_enabled:
        raise HTTPException(
            status_code=410,
            detail="Per-session event streaming is disabled. Poll session status and fetch artifacts separately.",
        )
    try:
        await container.sessions.read(session_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found.") from error
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


# Work on a stored session is queued, never run in the request. The three
# routes below answer 202 with the session (already ``queued``) and the job;
# the session card gauges progress from the list poll and unlocks on a
# terminal status. Running a pipeline inside a handler held the connection
# for as long as transcription took, and a restart mid-way left the session
# ``processing`` with no job row for startup recovery to find.


@router.post("/{session_id}/auto-crop", status_code=202)
async def auto_crop_session(session_id: str, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    try:
        return await container.session_maintenance.start_auto_crop(session_id)
    except Exception as error:
        raise http_error(error, fallback_message="Auto-crop could not be queued.", not_found_message="Session not found.") from error


@router.post("/{session_id}/process", status_code=202)
async def process_session(session_id: str, container: AppContainer = Depends(get_container)) -> dict[str, object]:
    try:
        return await container.session_maintenance.start_processing(session_id)
    except Exception as error:
        raise http_error(error, fallback_message="Processing could not be queued.", not_found_message="Session not found.") from error


@router.post("/{session_id}/clips/manual", status_code=202)
async def create_manual_clips(
    session_id: str,
    payload: ManualClipsRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    """Record a clip-export plan and queue the export job.

    202, not 200: the response carries the draft segmentation and a job id, and
    the MP4s are cut afterwards. Cutting them here would hold the connection for
    minutes on a long recording and lose everything on a restart. Poll the
    session's ``clipExport`` for progress.
    """
    try:
        return await container.clips.request_clip_export(
            session_id, payload.boundaries, payload.labels, payload.kinds
        )
    except Exception as error:
        raise http_error(error, fallback_message="Manual clip split failed.", not_found_message="Session not found.") from error


@router.post("/{session_id}/clips/{clip_id}/recrop", status_code=202)
async def recrop_clip(
    session_id: str,
    clip_id: str,
    payload: RecropClipRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    """Move a clip's boundaries and queue the job that re-cuts its MP4.

    202, not 200: the response carries the clip as a draft with its new range
    and a job id; the MP4 is cut afterwards by the same ``export_clips`` job a
    full split uses. Cutting it here held the connection for minutes on a long
    station and lost the work on a restart. Poll the session's ``clipExport``
    for progress.
    """
    try:
        return await container.clips.request_clip_recrop(session_id, clip_id, payload.start, payload.end)
    except Exception as error:
        raise http_error(error, fallback_message="Recrop failed.", not_found_message="Session not found.") from error


@router.post("/{session_id}/clips/{clip_id}/assess", status_code=202)
async def assess_clip(
    session_id: str,
    clip_id: str,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    """Create the clip's child session and queue its assessment.

    Always deferred: the former ``?defer`` switch is accepted and ignored so
    older clients keep working, but nothing scores inside the request any more.
    """
    try:
        return await container.clips.assess_clip(session_id, clip_id)
    except Exception as error:
        raise http_error(error, fallback_message="Clip assessment could not be queued.", not_found_message="Session not found.") from error


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
        raise http_error(error, fallback_message="Failed to rename clip.", not_found_message="Session not found.") from error
