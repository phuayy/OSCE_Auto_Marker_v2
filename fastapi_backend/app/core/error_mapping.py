"""One answer to "what status and public message does this exception get?".

Both the HTTP layer (``app.api.errors.http_error``) and services that report a
failure per item rather than raising (``SessionBulkService``) need it, and a
service must not import the API layer, so the classification lives here and
``http_error`` only wraps it in an ``HTTPException``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from app.core.exceptions import AppError

logger = logging.getLogger(__name__)

GENERIC_SERVER_ERROR = "Unexpected server error."


@dataclass(frozen=True)
class ErrorOutcome:
    status_code: int
    message: str
    # Known only for an ``AppError`` (whether the same call is worth retrying).
    retryable: bool | None = None


def classify_error(
    error: Exception,
    *,
    fallback_status: int = 500,
    fallback_message: str = GENERIC_SERVER_ERROR,
    not_found_message: str = "Not found.",
) -> ErrorOutcome:
    """Map ``error`` to a status and a message safe to show a client.

    ``AppError`` and the two exception types the services raise deliberately
    (``FileNotFoundError`` for a missing record, ``ValueError`` for a rejected
    argument) keep messages written for the caller. Anything else is
    unexpected: it is logged in full with a short reference id, and the client
    receives only that id, so a support request can be tied to the stack trace
    without publishing it.
    """
    if isinstance(error, AppError):
        return ErrorOutcome(error.status_code, error.message, error.retryable)
    if isinstance(error, FileNotFoundError):
        # str() on a FileNotFoundError includes the path it failed to open, so
        # the caller-facing message is used instead of the exception's own.
        return ErrorOutcome(404, not_found_message)
    if isinstance(error, ValueError):
        # Raised by validators and normalisers with a message meant for the
        # caller; a ValueError from anywhere else still reads as a bad request,
        # which is the honest status for input we could not use.
        return ErrorOutcome(400, str(error) or "Invalid request.")

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
        return ErrorOutcome(fallback_status, f"{fallback_message} Reference: {reference}.")
    return ErrorOutcome(fallback_status, fallback_message)
