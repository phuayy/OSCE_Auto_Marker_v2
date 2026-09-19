"""Shared translation from an exception to an HTTP response.

Three route modules had grown near-identical copies of this, each ending in
``detail=str(error)`` for the 500 case. That last line is the problem: an
unexpected exception's text is written for a developer, not a client, and here
it reached the browser — absolute filesystem paths, ffmpeg and WhisperX stderr,
SQLAlchemy statements with their parameters. Anything reportable is deliberate
and carries an :class:`AppError`; everything else is a bug, and a bug's detail
belongs in the log with a correlation id, not in the response body.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import HTTPException

from app.core.exceptions import AppError


logger = logging.getLogger(__name__)

GENERIC_SERVER_ERROR = "Unexpected server error."


def error_response_content(message: str, *, retryable: bool | None = None) -> dict[str, Any]:
    """The JSON body shape for a rejected request.

    Shared by both exception handlers in main.py (``AppError`` raised
    directly, and the ``HTTPException`` ``http_error`` converts it to) so
    ``retryable`` means the same thing — present only when actually known —
    regardless of which of the two a given route happens to go through.
    Every route in this codebase currently converts via ``http_error``
    (see its module docstring), so that is the path that matters in
    practice, but a bare ``raise AppError(...)`` reaches the client with the
    same shape.
    """
    content: dict[str, Any] = {"error": message}
    if retryable is not None:
        content["retryable"] = retryable
    return content


def http_error(
    error: Exception,
    *,
    fallback_status: int = 500,
    fallback_message: str = GENERIC_SERVER_ERROR,
    not_found_message: str = "Not found.",
) -> HTTPException:
    """Map ``error`` to an :class:`HTTPException` safe to return to a client.

    ``AppError`` and the two exception types the services raise deliberately
    (``FileNotFoundError`` for a missing record, ``ValueError`` for a rejected
    argument) keep their own messages — those are written for the caller.

    Anything else is unexpected. It is logged in full with a short reference id,
    and the client receives only that id, so a support request can be tied to
    the stack trace without publishing it.
    """
    if isinstance(error, AppError):
        response = HTTPException(status_code=error.status_code, detail=error.message)
        # Not an HTTPException field; attached so the generic handler in
        # main.py can still tell a caller "this exact conflict is worth
        # retrying" (StaleSessionError, from SessionService.update's retry
        # loop) apart from a rejection that will fail identically again —
        # without it every AppError raised inside a route's own try/except
        # loses that distinction the moment it is converted here.
        response.retryable = error.retryable  # type: ignore[attr-defined]
        return response
    if isinstance(error, FileNotFoundError):
        # str() on a FileNotFoundError includes the path it failed to open, so
        # the caller-facing message is used instead of the exception's own.
        return HTTPException(status_code=404, detail=not_found_message)
    if isinstance(error, ValueError):
        # Raised by validators and normalisers with a message meant for the
        # caller; a ValueError from anywhere else still reads as a bad request,
        # which is the honest status for input we could not use.
        return HTTPException(status_code=400, detail=str(error) or "Invalid request.")

    if fallback_status >= 500:
        reference = uuid.uuid4().hex[:12]
        # exc_info is passed explicitly rather than using logger.exception():
        # that only captures a traceback while an exception is being handled, so
        # a call from outside an `except` block would log the reference with no
        # detail at all — the response would point at a log entry that does not
        # contain the failure it references.
        logger.error(
            "Unhandled error returned as HTTP %d (reference %s): %s",
            fallback_status,
            reference,
            error,
            exc_info=error,
            extra={"trace_id": reference, "stage": "http_error"},
        )
        return HTTPException(
            status_code=fallback_status,
            detail=f"{fallback_message} Reference: {reference}.",
        )
    return HTTPException(status_code=fallback_status, detail=fallback_message)
