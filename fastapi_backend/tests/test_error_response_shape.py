"""The client-facing shape of a rejected request, and specifically whether it
can tell a conflict worth retrying (StaleSessionError, from
SessionService.update's retry loop) apart from a rejection that will fail
identically again.

Every route wraps its own AppErrors through app/api/errors.py::http_error
before raising (see that module's docstring), so the HTTPException handler —
not the AppError one — is the path that actually carries production traffic;
both are exercised here because a bug in either would silently drop the flag.

These call the real handlers registered on the real `app.main.app`, not a
reimplementation, but never start it: FastAPI stores exception handlers in a
plain dict (`app.exception_handlers`) that exists once the module has been
imported and the object constructed — no lifespan, no container, no I/O.
The route-guard tests rely on that same fact; tests/fixtures/app_routes.py
imports the app once for all of them.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import HTTPException

from app.api.errors import http_error
from app.core.exceptions import AppError, StaleSessionError, TranscriptionResourceError
from tests.fixtures.app_routes import app


def _handle(exc_type: type, exc: Exception):
    handler = app.exception_handlers[exc_type]
    response = asyncio.run(handler(None, exc))
    return response.status_code, json.loads(response.body)


def test_a_retryable_app_error_says_so_when_raised_directly() -> None:
    status, body = _handle(AppError, StaleSessionError("s1", loaded_version="a", current_version="b"))
    assert status == 409
    assert body["retryable"] is True


def test_a_non_retryable_app_error_says_so_when_raised_directly() -> None:
    status, body = _handle(AppError, TranscriptionResourceError("The host is out of memory."))
    assert status == 507
    assert body["retryable"] is False


def test_the_path_every_route_actually_takes_keeps_the_flag() -> None:
    """AppError -> http_error() -> HTTPException -> this handler. This is how
    a session-update conflict really reaches the browser."""
    converted = http_error(StaleSessionError("s1", loaded_version="a", current_version="b"))
    status, body = _handle(HTTPException, converted)
    assert status == 409
    assert body["error"] == "Session s1 changed while it was being edited; the edit was not applied."
    assert body["retryable"] is True


def test_an_ordinary_http_exception_carries_no_retryable_field() -> None:
    """Not merely false — absent, so a client can tell "we don't know" apart
    from "we checked, and no". A plain HTTPException (a 404 from an ownership
    dependency, say) never passed through http_error()'s AppError branch."""
    status, body = _handle(HTTPException, HTTPException(status_code=404, detail="Not found."))
    assert status == 404
    assert "retryable" not in body
