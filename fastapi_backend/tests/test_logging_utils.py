from __future__ import annotations

import logging

from app.core.logging_utils import (
    RedactStreamTicketFilter,
    install_access_log_redaction,
)


def _record(args: tuple) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s" %d',
        args=args,
        exc_info=None,
    )


def test_ticket_query_value_is_redacted() -> None:
    record = _record(("127.0.0.1:5000", 'GET /media/source/video.mp4?ticket=abc.def-123_TOKEN HTTP/1.1', 200))
    assert RedactStreamTicketFilter().filter(record) is True
    assert "abc.def-123_TOKEN" not in record.args[1]
    assert "ticket=<redacted>" in record.args[1]


def test_a_ticket_value_followed_by_another_query_param_is_still_bounded() -> None:
    record = _record(("127.0.0.1:5000", "GET /api/sessions/s1/events?ticket=SECRET&foo=bar HTTP/1.1", 200))
    RedactStreamTicketFilter().filter(record)
    assert "SECRET" not in record.args[1]
    assert "ticket=<redacted>&foo=bar" in record.args[1]


def test_a_request_line_with_no_ticket_is_unchanged() -> None:
    line = "GET /api/sessions HTTP/1.1"
    record = _record(("127.0.0.1:5000", line, 200))
    RedactStreamTicketFilter().filter(record)
    assert record.args[1] == line


def test_non_string_args_pass_through_untouched() -> None:
    record = _record(("127.0.0.1:5000", "GET /api/health HTTP/1.1", 200))
    RedactStreamTicketFilter().filter(record)
    assert record.args[2] == 200


def test_install_is_idempotent() -> None:
    """Calling this more than once (a --reload child re-imports app.main, and
    other test modules importing app.main already trigger one install) must
    never accumulate duplicate filter instances on the shared logger."""
    access_logger = logging.getLogger("uvicorn.access")
    install_access_log_redaction()
    install_access_log_redaction()
    count = sum(1 for existing in access_logger.filters if isinstance(existing, RedactStreamTicketFilter))
    assert count == 1
