"""The start gate: a tiny always-available page that switches the marker's VM
on and shows how far the start has got.

Runs on Cloud Run with min-instances 0 (it costs nothing while nobody looks
at it). The marker itself is on a GCE VM that switches itself off when idle
(``IDLE_SHUTDOWN_MINUTES`` in the app). A person opens this page, sees
whether the marker is running, signs in with Google if it is not, presses
Start, watches the stages, and is sent to the marker's own login page once
it answers.

Endpoints:

* ``GET  /``             the page (static/index.html)
* ``GET  /api/config``   what the page needs to render sign-in
* ``GET  /api/status``   the stage, open to anyone: "is it running?" needs no account
* ``POST /api/session``  Google ID token in, signed cookie out
* ``POST /api/logout``
* ``POST /api/start``    signed-in and allowlisted only
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse

from osce_gate.auth import (
    COOKIE_NAME,
    Allowlist,
    SessionCookie,
    SignInError,
    email_from_claims,
    google_token_verifier,
)
from osce_gate.compute import ComputeClient, InstanceRef, RestComputeClient
from osce_gate.history import GcsHistory, MemoryHistory, StartHistory
from osce_gate.stages import Observation, describe, parse_boot_value, parse_timestamp

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_STOPPED = {"TERMINATED", "STOPPED", "SUSPENDED"}


@dataclass(frozen=True)
class GateConfig:
    project: str
    zone: str
    instance: str
    app_url: str
    probe_url: str
    google_client_id: str
    allowlist: Allowlist
    session_secret: str
    history_bucket: str = ""
    expected_start_seconds: int = 240
    app_name: str = "OSCE AI Marker"
    cookie_secure: bool = True

    @classmethod
    def from_env(cls) -> "GateConfig":
        def need(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise RuntimeError(f"{name} is required (see deploy/gcp/README.md).")
            return value

        app_url = need("GATE_APP_URL").rstrip("/")
        return cls(
            project=need("GATE_PROJECT"),
            zone=need("GATE_ZONE"),
            instance=need("GATE_INSTANCE"),
            app_url=app_url,
            probe_url=(os.getenv("GATE_PROBE_URL", "").strip() or app_url).rstrip("/"),
            google_client_id=need("GATE_GOOGLE_CLIENT_ID"),
            allowlist=Allowlist.parse(os.getenv("GATE_ALLOWED_EMAILS", ""), os.getenv("GATE_ALLOWED_DOMAINS", "")),
            session_secret=need("GATE_SESSION_SECRET"),
            history_bucket=os.getenv("GATE_HISTORY_BUCKET", "").strip(),
            expected_start_seconds=int(os.getenv("GATE_EXPECTED_START_SECONDS", "240") or 240),
            app_name=os.getenv("GATE_APP_NAME", "").strip() or "OSCE AI Marker",
            cookie_secure=os.getenv("GATE_COOKIE_SECURE", "true").strip().lower() not in {"0", "false", "no"},
        )


def probe_ready(probe_url: str) -> bool:
    """The app's own readiness probe. The only thing that may say "ready"."""
    try:
        response = httpx.get(f"{probe_url}/api/health/ready", timeout=3.0)
        return response.status_code == 200 and bool(response.json().get("ready"))
    except Exception:
        return False


def create_app(
    config: GateConfig,
    *,
    compute: ComputeClient | None = None,
    verify_token: Callable[[str], dict[str, Any]] | None = None,
    history: StartHistory | None = None,
    probe: Callable[[str], bool] = probe_ready,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    if not config.allowlist.emails and not config.allowlist.domains:
        logger.warning("GATE_ALLOWED_EMAILS and GATE_ALLOWED_DOMAINS are both empty: nobody can start the marker.")
    compute = compute or RestComputeClient(InstanceRef(config.project, config.zone, config.instance))
    verify_token = verify_token or google_token_verifier(config.google_client_id)
    history = history or (GcsHistory(config.history_bucket) if config.history_bucket else MemoryHistory())
    cookies = SessionCookie(config.session_secret)

    app = FastAPI(title="OSCE marker start gate", docs_url=None, redoc_url=None, openapi_url=None)

    def signed_in(request: Request) -> str | None:
        return cookies.read(request.cookies.get(COOKIE_NAME), config.allowlist)

    @app.middleware("http")
    async def no_store(request: Request, call_next):
        response = await call_next(request)
        # Status changes by the second; a cached answer is a wrong one.
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/api/config")
    def page_config(request: Request) -> dict[str, Any]:
        return {
            "appName": config.app_name,
            "googleClientId": config.google_client_id,
            "email": signed_in(request),
        }

    @app.get("/api/status")
    def status(request: Request, operation: str = "") -> dict[str, Any]:
        try:
            instance = compute.get_instance()
            start_error = compute.operation_error(operation) if operation else None
            boot_raw = compute.get_boot_attribute() if instance.status.upper() == "RUNNING" else None
        except Exception as error:
            logger.exception("Could not read the instance state.")
            raise HTTPException(status_code=502, detail="Could not read the machine's state. Try again shortly.") from error
        last_start = parse_timestamp(instance.last_start_timestamp)
        boot = parse_boot_value(boot_raw)
        ready = instance.status.upper() == "RUNNING" and probe(config.probe_url)
        observation = Observation(
            instance_status=instance.status,
            last_start=last_start,
            boot=boot,
            app_ready=ready,
            start_requested=bool(operation),
            start_error=start_error,
        )
        if ready and boot is not None and boot.stage == "ready" and last_start is not None and boot.at >= last_start:
            history.record(instance.last_start_timestamp or "", boot.at - last_start)
        body = describe(
            observation,
            now=clock(),
            history=history.durations(),
            default_estimate_seconds=config.expected_start_seconds,
        )
        body["appUrl"] = config.app_url
        body["email"] = signed_in(request)
        return body

    @app.post("/api/session")
    def sign_in(response: Response, credential: str = Body(..., embed=True)) -> dict[str, Any]:
        try:
            claims = verify_token(credential)
        except Exception as error:
            raise HTTPException(status_code=401, detail="Google sign-in could not be verified. Please try again.") from error
        try:
            email = email_from_claims(claims, config.allowlist)
        except SignInError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        response.set_cookie(
            COOKIE_NAME,
            cookies.issue(email),
            max_age=cookies.ttl_seconds,
            httponly=True,
            secure=config.cookie_secure,
            samesite="strict",
        )
        return {"email": email}

    @app.post("/api/logout")
    def sign_out(response: Response) -> dict[str, bool]:
        response.delete_cookie(COOKIE_NAME)
        return {"ok": True}

    @app.post("/api/start")
    def start(request: Request) -> dict[str, Any]:
        email = signed_in(request)
        if email is None:
            raise HTTPException(status_code=401, detail="Sign in to start the marker.")
        try:
            instance = compute.get_instance()
            if instance.status.upper() not in _STOPPED:
                # Already starting, running or stopping: pressing Start twice,
                # or two people pressing it at once, is not an error.
                return {"started": False, "operation": "", "status": instance.status}
            operation = compute.start_instance()
        except Exception as error:
            logger.exception("Could not start the instance.")
            raise HTTPException(status_code=502, detail="The machine could not be started. Try again shortly.") from error
        logger.info("Start requested by %s (operation %s).", email, operation)
        return {"started": True, "operation": operation, "status": instance.status}

    return app


def build_from_env() -> FastAPI:
    logging.basicConfig(level=logging.INFO)
    return create_app(GateConfig.from_env())
