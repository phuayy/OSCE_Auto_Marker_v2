from __future__ import annotations

from typing import Any


def log_context(trace_id: str, stage: str, **extra: Any) -> dict[str, Any]:
    """Build a structured-logging ``extra`` dict with consistent keys.

    Use as ``logger.info("msg", extra=log_context(session_id, "whisperx", ...))``
    so every pipeline log line carries a correlation id (``trace_id``) and the
    pipeline ``stage``, making prod logs searchable and joinable across stages.
    """
    return {"trace_id": trace_id, "stage": stage, **extra}
