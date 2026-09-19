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
from app.api.errors import error_response_content
from app.api.frontend import mount_frontend
from app.api.routes import (
    analytics,
    async_uploads,
    auth,
    corpora,
    events as events_routes,
    health,
    jobs,
    notifications,
    rubrics,
    sessions,
    settings as settings_routes,
    users as users_routes,
    webhooks,
)
from app.core.asyncio_compat import configure_windows_selector_event_loop_policy
from app.core.config import Settings, settings
from app.core.exceptions import AppError
from app.core.logging_utils import install_access_log_redaction
from app.core.security_headers import apply_security_headers
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
    # uvicorn's own logging setup runs before this module is imported (see
    # module docstring order in scripts/run_api.py), so uvicorn.access already
    # exists by the time this filter is attached.
    install_access_log_redaction()


configure_windows_selector_event_loop_policy()
_configure_app_logging()

# One process, one env snapshot: `app.core.config` resolves `Settings.load()`
# once at import time and every process-wide caller shares that instance
# rather than re-resolving its own (each `Settings.load()` call re-globs the
# winget ffmpeg directories and re-probes binaries on PATH — harmless work,
# but pointless to repeat, and a second instance is a second place the two
# could silently disagree if `load()` ever grows a non-deterministic step).
# `Settings` itself stays exported for callers that legitimately want an
# independent instance (tests, `Depends` overrides).


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    container: AppContainer = create_container()
    application.state.container = container
    try:
        await container.startup()
        yield
    finally:
        await container.shutdown()


def build_app(app_settings: Settings) -> FastAPI:
    """Construct the FastAPI app for one ``Settings`` instance.

    A function rather than module-level statements so a test can build the
    app against a settings value it controls (``api_docs_enabled`` in
    particular) without re-executing this module under a patched
    environment. The module-level ``app`` below is just ``build_app(settings)``
    — ``uvicorn app.main:app`` is unchanged.
    """
    application = FastAPI(
        title="OSCE AI Marker API",
        version="0.1.0",
        lifespan=lifespan,
        # /docs, /redoc and /openapi.json map this deployment's entire API —
        # every admin route included — with no authentication of their own;
        # authorize_request's open-path list only ever exempted /api/health
        # and /api/auth/*, never these. Off by default, like
        # PROTECT_MEDIA_ENDPOINTS; collect_runtime_warnings() flags it when on.
        docs_url="/docs" if app_settings.api_docs_enabled else None,
        redoc_url="/redoc" if app_settings.api_docs_enabled else None,
        openapi_url="/openapi.json" if app_settings.api_docs_enabled else None,
    )

    @application.middleware("http")
    async def require_auth(request: Request, call_next):
        container: AppContainer = request.app.state.container
        allowed, payload = await authorize_request(
            request,
            container,
            protect_media=app_settings.protect_media_endpoints,
        )
        if not allowed:
            return JSONResponse(status_code=401, content={"error": "Authentication required."})
        if payload is not None:
            request.state.auth_user = payload
        return await call_next(request)

    # Added after require_auth so it wraps it (Starlette's user middleware
    # nests in reverse registration order — the most recently added is
    # outermost, running first on the way in). A cross-origin preflight
    # (`OPTIONS` with `Origin` + `Access-Control-Request-Method`) carries no
    # bearer token by design — the browser sends it before the real request
    # even exists — so it must reach CORSMiddleware's own short-circuit before
    # require_auth ever sees it, or every preflight to a protected route dies
    # as a 401 and the browser never sends the real request. Registering this
    # after require_auth is what makes that true; a same-origin deployment
    # (the documented shape — `SERVE_FRONTEND=true`, no `Origin` header on
    # same-origin requests) never exercises this path at all.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(app_settings.cors_allow_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Added last so it wraps everything, including the two middlewares above:
    # every response gets these headers, a CORS preflight or a 401 from
    # require_auth included, rather than only the ones that reach a route
    # handler.
    @application.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        apply_security_headers(response.headers, app_settings)
        return response

    @application.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
        # `retryable` is set only on an HTTPException that started life as an
        # AppError and was converted by app/api/errors.py::http_error — the
        # path every route actually takes (see that module's docstring).
        # Absent (not merely false) for everything else, so a client can tell
        # "we don't know" apart from "we checked, and no".
        retryable = getattr(exc, "retryable", None)
        return JSONResponse(
            status_code=exc.status_code,
            content=error_response_content(detail, retryable=retryable),
        )

    @application.exception_handler(AppError)
    async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        # A defense-in-depth handler: every route wraps its own AppErrors
        # through http_error() before raising (see above), so this fires only
        # for one raised outside that try/except — middleware, a dependency,
        # or a bug in a route that forgot the wrapper. Shares the same content
        # shape so a client never has to tell the two paths apart.
        return JSONResponse(
            status_code=exc.status_code,
            content=error_response_content(exc.message, retryable=exc.retryable),
        )

    @application.exception_handler(RequestValidationError)
    async def validation_exception_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        first_error = exc.errors()[0] if exc.errors() else {}
        message = str(first_error.get("msg") or "Invalid request payload.")
        return JSONResponse(status_code=400, content={"error": message})

    application.mount(
        "/media/videos", StaticFiles(directory=str(app_settings.paths.input_videos_dir), check_dir=False), name="media-videos"
    )
    application.mount(
        "/media/audio", StaticFiles(directory=str(app_settings.paths.output_audio_dir), check_dir=False), name="media-audio"
    )
    application.mount(
        "/media/audio-professionalism",
        StaticFiles(directory=str(app_settings.paths.output_audio_professionalism_dir), check_dir=False),
        name="media-audio-professionalism",
    )
    application.mount(
        "/media/whisperx",
        StaticFiles(directory=str(app_settings.paths.output_whisperx_dir), check_dir=False),
        name="media-whisperx",
    )
    application.mount(
        "/media/transcripts",
        StaticFiles(directory=str(app_settings.paths.output_transcripts_dir), check_dir=False),
        name="media-transcripts",
    )
    application.mount(
        "/media/scores", StaticFiles(directory=str(app_settings.paths.output_scores_dir), check_dir=False), name="media-scores"
    )
    application.mount(
        "/media/communication-scores",
        StaticFiles(directory=str(app_settings.paths.output_communication_scores_dir), check_dir=False),
        name="media-communication-scores",
    )
    application.mount(
        "/media/clips", StaticFiles(directory=str(app_settings.paths.output_clips_dir), check_dir=False), name="media-clips"
    )
    application.mount(
        "/media/source", StaticFiles(directory=str(app_settings.object_storage_root), check_dir=False), name="media-source"
    )

    application.include_router(health.router, prefix="/api")
    application.include_router(health.admin_router, prefix="/api")
    application.include_router(events_routes.router, prefix="/api")
    application.include_router(auth.router, prefix="/api")
    application.include_router(users_routes.router, prefix="/api")
    application.include_router(rubrics.router, prefix="/api")
    application.include_router(rubrics.admin_router, prefix="/api")
    application.include_router(async_uploads.router, prefix="/api")
    application.include_router(jobs.router, prefix="/api")
    application.include_router(sessions.router, prefix="/api")
    application.include_router(analytics.router, prefix="/api")
    application.include_router(notifications.router, prefix="/api")
    application.include_router(webhooks.router, prefix="/api")
    application.include_router(corpora.router, prefix="/api")
    application.include_router(corpora.admin_router, prefix="/api")
    application.include_router(settings_routes.router, prefix="/api")
    application.include_router(settings_routes.admin_router, prefix="/api")

    # Last on purpose: a catch-all for the built frontend can only serve what
    # the routers and media mounts above did not claim.
    mount_frontend(application, app_settings)
    return application


app = build_app(settings)
