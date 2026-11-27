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

from typing import Any

from fastapi import HTTPException

from app.core.error_mapping import GENERIC_SERVER_ERROR, classify_error


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

    The classification itself is ``app.core.error_mapping.classify_error``
    (shared with services that report failures per item); this wraps it in an
    ``HTTPException`` and lets a deliberate one pass through.
    """
    if isinstance(error, HTTPException):
        # A deliberate HTTP refusal raised inside a route's own try/except —
        # e.g. ``MaxBodySizeMiddleware``'s ``RequestBodyTooLarge`` surfacing
        # through a handler that reads the body itself and converts whatever
        # it catches via this function — must pass through unchanged. Without
        # this it falls through to the generic 500 branch below and a 413
        # the caller already decided on turns into "Unexpected server error."
        return error
    outcome = classify_error(
        error,
        fallback_status=fallback_status,
        fallback_message=fallback_message,
        not_found_message=not_found_message,
    )
    response = HTTPException(status_code=outcome.status_code, detail=outcome.message)
    if outcome.retryable is not None:
        # Not an HTTPException field; attached so the generic handler in
        # main.py can still tell a caller "this exact conflict is worth
        # retrying" (StaleSessionError, from SessionService.update's retry
        # loop) apart from a rejection that will fail identically again —
        # without it every AppError raised inside a route's own try/except
        # loses that distinction the moment it is converted here.
        response.retryable = outcome.retryable  # type: ignore[attr-defined]
    return response
