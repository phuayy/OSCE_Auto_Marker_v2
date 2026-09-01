"""An unexpected exception's text must not reach the client.

The route modules previously ended in ``detail=str(error)`` for the 500 case,
which published whatever the failure happened to say: absolute filesystem paths,
ffmpeg and WhisperX stderr, SQLAlchemy statements with their bound parameters.
Deliberate, caller-facing rejections still carry their own message — the
distinction these tests pin.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from app.api.errors import GENERIC_SERVER_ERROR, http_error
from app.core.exceptions import AppError, EmptyTranscriptError


_LEAKY_MESSAGE = (
    r"FATAL: could not open D:\srv\osce\storage\auth\secret.key "
    "(psycopg OperationalError: password authentication failed for user 'osce_app')"
)


def test_unexpected_errors_do_not_reach_the_client() -> None:
    result = http_error(RuntimeError(_LEAKY_MESSAGE))

    assert isinstance(result, HTTPException)
    assert result.status_code == 500
    detail = str(result.detail)
    assert GENERIC_SERVER_ERROR in detail
    # None of the internal specifics survive.
    assert "secret.key" not in detail
    assert "psycopg" not in detail
    assert "password" not in detail
    assert "osce_app" not in detail


def test_a_reference_id_ties_the_response_to_the_log(caplog) -> None:
    """The detail must still be actionable: support needs to find the trace."""
    with caplog.at_level(logging.ERROR):
        result = http_error(RuntimeError(_LEAKY_MESSAGE))

    detail = str(result.detail)
    reference = detail.rsplit("Reference: ", 1)[1].rstrip(".")
    assert len(reference) == 12
    # The full text is logged, under the same reference.
    assert reference in caplog.text
    assert "secret.key" in caplog.text


def test_two_failures_get_distinct_references() -> None:
    first = str(http_error(RuntimeError("a")).detail)
    second = str(http_error(RuntimeError("b")).detail)
    assert first != second


def test_app_errors_keep_their_message_and_status() -> None:
    """These are written for the caller, so they pass through verbatim."""
    result = http_error(AppError("Upload session has expired.", status_code=410))

    assert result.status_code == 410
    assert result.detail == "Upload session has expired."


def test_domain_app_error_subclasses_pass_through() -> None:
    result = http_error(EmptyTranscriptError("Transcription produced no speech."))

    assert result.status_code == 422
    assert result.detail == "Transcription produced no speech."


def test_missing_records_return_the_callers_message_not_the_path() -> None:
    """str(FileNotFoundError) includes the path it failed to open, so the
    caller-facing message is used instead of the exception's own."""
    error = FileNotFoundError(r"D:\srv\osce\storage\sessions\abc.json")
    result = http_error(error, not_found_message="Session not found.")

    assert result.status_code == 404
    assert result.detail == "Session not found."
    assert "storage" not in str(result.detail)


def test_value_errors_are_client_errors_with_their_message() -> None:
    result = http_error(ValueError("Session name is required."))

    assert result.status_code == 400
    assert result.detail == "Session name is required."


def test_a_value_error_without_a_message_still_reads_as_a_bad_request() -> None:
    result = http_error(ValueError())

    assert result.status_code == 400
    assert result.detail == "Invalid request."


def test_a_non_5xx_fallback_is_not_given_a_reference() -> None:
    """A reference id is for finding a stack trace; a 4xx has none to find."""
    result = http_error(
        RuntimeError("internal detail"), fallback_status=409, fallback_message="Conflict."
    )

    assert result.status_code == 409
    assert result.detail == "Conflict."
    assert "internal detail" not in str(result.detail)
    assert "Reference" not in str(result.detail)
