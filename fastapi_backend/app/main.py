from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.dependencies import authorize_request
from app.api.routes import analytics, async_uploads, auth, health, jobs, rubrics, sessions, uploads
from app.core.asyncio_compat import configure_windows_selector_event_loop_policy
from app.core.config import Settings
from app.core.exceptions import AppError
from app.services.container import AppContainer, create_container


def _configure_app_logging() -> None:
    """Surface the application's own ``app.*`` loggers in the API console.

    uvicorn only configures its own loggers, leaving the root logger at WARNING —
    so app INFO logs (job dispatched/queued, pipeline steps, subprocess timing)
    were silently dropped in the API process. Attach a stream handler to the
    ``app`` logger so those are visible, matching the worker's output.
    """
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    app_logger = logging.getLogger("app")
    app_logger.setLevel(level)
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
        app_logger.addHandler(handler)
    app_logger.propagate = False


configure_windows_selector_event_loop_policy()
_configure_app_logging()

settings = Settings.load()


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    container: AppContainer = create_container()
    application.state.container = container
    await container.startup()
    try:
        yield
    finally:
        await container.shutdown()


app = FastAPI(title="OSCE AI Marker API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_allow_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def require_auth(request: Request, call_next):
    container: AppContainer = request.app.state.container
    allowed, payload = authorize_request(
        request,
        container,
        protect_media=settings.protect_media_endpoints,
    )
    if not allowed:
        return JSONResponse(status_code=401, content={"error": "Authentication required."})
    if payload is not None:
        request.state.auth_user = payload
    return await call_next(request)


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
    return JSONResponse(status_code=exc.status_code, content={"error": detail})


@app.exception_handler(AppError)
async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.message})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    first_error = exc.errors()[0] if exc.errors() else {}
    message = str(first_error.get("msg") or "Invalid request payload.")
    return JSONResponse(status_code=400, content={"error": message})


app.mount("/media/videos", StaticFiles(directory=str(settings.paths.input_videos_dir), check_dir=False), name="media-videos")
app.mount("/media/audio", StaticFiles(directory=str(settings.paths.output_audio_dir), check_dir=False), name="media-audio")
app.mount(
    "/media/audio-professionalism",
    StaticFiles(directory=str(settings.paths.output_audio_professionalism_dir), check_dir=False),
    name="media-audio-professionalism",
)
app.mount("/media/whisperx", StaticFiles(directory=str(settings.paths.output_whisperx_dir), check_dir=False), name="media-whisperx")
app.mount(
    "/media/transcripts",
    StaticFiles(directory=str(settings.paths.output_transcripts_dir), check_dir=False),
    name="media-transcripts",
)
app.mount("/media/scores", StaticFiles(directory=str(settings.paths.output_scores_dir), check_dir=False), name="media-scores")
app.mount(
    "/media/communication-scores",
    StaticFiles(directory=str(settings.paths.output_communication_scores_dir), check_dir=False),
    name="media-communication-scores",
)
app.mount("/media/clips", StaticFiles(directory=str(settings.paths.output_clips_dir), check_dir=False), name="media-clips")
app.mount("/media/source", StaticFiles(directory=str(settings.object_storage_root), check_dir=False), name="media-source")

app.include_router(health.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(rubrics.router, prefix="/api")
app.include_router(uploads.router, prefix="/api")
app.include_router(async_uploads.router, prefix="/api")
app.include_router(jobs.router, prefix="/api")
app.include_router(sessions.router, prefix="/api")
app.include_router(analytics.router, prefix="/api")
