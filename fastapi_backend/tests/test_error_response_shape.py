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
test_admin_routes_are_guarded.py already relies on that same fact to walk
`app.routes` safely.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import HTTPException

from app.api.errors import http_error
from app.core.exceptions import AppError, StaleSessionError, TranscriptionResourceError

# Importing app.main runs its logging setup, which stops the "app" logger
# propagating to the root — and pytest's caplog listens on the root. Save and
# restore what the import changes, the same guard
# test_admin_routes_are_guarded.py uses, so tests that run after this module
# in the same session still see the log lines they assert on.
_app_logger = logging.getLogger("app")
_saved_logging = (_app_logger.propagate, list(_app_logger.handlers), _app_logger.level)
from app.main import app  # noqa: E402

_app_logger.propagate, _app_logger.handlers[:], _ = _saved_logging
_app_logger.setLevel(_saved_logging[2])


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
