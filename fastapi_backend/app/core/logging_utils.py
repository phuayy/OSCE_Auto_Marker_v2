from __future__ import annotations

import logging
import re
from typing import Any


def log_context(trace_id: str, stage: str, **extra: Any) -> dict[str, Any]:
    """Build a structured-logging ``extra`` dict with consistent keys.

    Use as ``logger.info("msg", extra=log_context(session_id, "whisperx", ...))``
    so every pipeline log line carries a correlation id (``trace_id``) and the
    pipeline ``stage``, making prod logs searchable and joinable across stages.
    """
    return {"trace_id": trace_id, "stage": stage, **extra}


# Media and SSE endpoints authenticate via a short-lived ``?ticket=`` query
# parameter because ``<video>``/``EventSource`` cannot send an Authorization
# header (see ``extract_stream_ticket`` in app/api/dependencies.py). Bearer
# tokens never travel this way, but the ticket does, and it lands verbatim in
# uvicorn's access log — and anywhere that log is shipped — for the whole of
# its TTL (STREAM_TICKET_TTL_SECONDS). This is the log-side mitigation;
# docs/deployment-vm.md separately documents that a reverse proxy's own access
# log carries the same value and needs the operator's own retention/access
# discipline, since this process cannot instrument a log line it never writes.
_TICKET_QUERY_PATTERN = re.compile(r"(?i)(\bticket=)[^&\s\"]+")


class RedactStreamTicketFilter(logging.Filter):
    """A ``logging.Filter`` that redacts ``ticket=...`` query values from a
    log record's formatting arguments before they reach any handler.

    uvicorn's access logger passes the request line (method, full path incl.
    query string, HTTP version) as a positional arg to ``logger.info(...)``,
    not as the already-formatted message, so redaction has to happen on
    ``record.args`` — inspecting ``record.getMessage()`` would be too late.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _TICKET_QUERY_PATTERN.sub(r"\1<redacted>", value) if isinstance(value, str) else value
                for value in record.args
            )
        elif isinstance(record.msg, str):
            record.msg = _TICKET_QUERY_PATTERN.sub(r"\1<redacted>", record.msg)
        return True


def install_access_log_redaction() -> None:
    """Attach :class:`RedactStreamTicketFilter` to uvicorn's access logger.

    Idempotent — safe to call once per process even though it may legitimately
    run more than once (a ``--reload`` child re-imports ``app.main``, and
    every worker process imports it fresh), because a logger only gains the
    filter instance once.
    """
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(existing, RedactStreamTicketFilter) for existing in access_logger.filters):
        access_logger.addFilter(RedactStreamTicketFilter())
