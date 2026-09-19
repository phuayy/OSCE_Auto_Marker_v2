"""Blanket declared-size cap for request bodies with no size contract of
their own.

Complements ``app.core.security_headers`` in the same "defense in depth"
sense: neither replaces real input validation, but a request that never
should have been read into memory in the first place is cheaper to refuse
before it is. Starlette buffers a request body in full before a route or a
pydantic model ever inspects it, so the check has to run *before* that
buffering — the same reasoning ``async_uploads.py::_reject_oversized_part``
already applies to one route (declared ``Content-Length`` over the limit is
rejected before ``request.body()`` is ever awaited). This module generalises
that pattern to every route that carries no size contract of its own,
instead of leaving each new JSON endpoint to remember it individually.

A pure ASGI middleware, not ``BaseHTTPMiddleware``: the check only needs the
request's headers, never its body, so there is nothing to gain from
Starlette's request/response wrapping and a good reason to avoid it — a
``BaseHTTPMiddleware`` subclass buffers the whole body internally the moment
anything downstream reads it, so it cannot itself avoid the very cost this
module exists to prevent.
"""

from __future__ import annotations

import re

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# PUT /api/uploads/{upload_id}/parts/{part_number} already enforces its own,
# larger cap on the bytes actually received (see the module docstring); it is
# the only route built to carry a multi-megabyte body and is exempt here.
_PART_UPLOAD_PATH = re.compile(r"^/api/uploads/[^/]+/parts/\d+$")


def _is_exempt(scope: Scope) -> bool:
    return scope.get("method") == "PUT" and bool(_PART_UPLOAD_PATH.match(scope.get("path", "")))


class MaxBodySizeMiddleware:
    """Rejects a request whose declared ``Content-Length`` exceeds ``max_bytes``.

    Declared-length only, deliberately: a client that lies about — or omits —
    ``Content-Length`` is not caught here, the same gap
    ``_reject_oversized_part`` leaves open for the one route that already
    does this (see its docstring). Closing that gap needs a streaming byte
    count enforced on every route's body consumption, which nothing in this
    codebase does today; this middleware protects the common case — every
    well-behaved client, and any request small enough to matter to a
    memory budget before a single byte is read — for the cost of an ASGI
    middleware with no state.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _is_exempt(scope):
            await self.app(scope, receive, send)
            return

        declared = next((value for key, value in scope.get("headers", []) if key == b"content-length"), None)
        if declared is not None:
            try:
                declared_bytes = int(declared)
            except ValueError:
                declared_bytes = None
            if declared_bytes is not None and declared_bytes > self.max_bytes:
                response = JSONResponse(
                    status_code=413,
                    content={"error": f"Request body exceeds the {self.max_bytes // (1024 * 1024)} MB limit."},
                )
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)


__all__ = ["MaxBodySizeMiddleware"]
