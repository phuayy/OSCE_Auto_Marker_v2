"""Cap for request bodies with no size contract of their own — declared and
actual size alike.

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
request's headers and the raw ASGI ``receive`` stream, never a parsed
request or response object, so there is nothing to gain from Starlette's
request/response wrapping and a good reason to avoid it — a
``BaseHTTPMiddleware`` subclass buffers the whole body internally the moment
anything downstream reads it, so it cannot itself avoid the very cost this
module exists to prevent.

Two limits, not one. ``PUT /api/uploads/{upload_id}/parts/{part_number}`` is
the one route built to carry a multi-megabyte body, so it is exempt from the
*generic* cap but not uncapped outright: it is capped at its own, larger
``part_max_bytes`` instead. A declared ``Content-Length`` over either limit is
rejected before a single byte is read, same as before; what is new is that a
body with no declared length — or one that lies about it — is now also
capped while it streams in, by literally counting the bytes ``receive()``
hands back frame by frame. A generator body sent chunked carries no
``Content-Length`` header at all, and nothing before this stopped it from
being buffered without bound.
"""

from __future__ import annotations

import re

from fastapi import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# PUT /api/uploads/{upload_id}/parts/{part_number} carries its own, larger
# cap (see the module docstring); it is exempt from the *generic* one here.
_PART_UPLOAD_PATH = re.compile(r"^/api/uploads/[^/]+/parts/\d+$")


def _is_part_upload(scope: Scope) -> bool:
    return scope.get("method") == "PUT" and bool(_PART_UPLOAD_PATH.match(scope.get("path", "")))


class RequestBodyTooLarge(HTTPException):
    """Raised mid-stream once the running byte count passes the route's limit.

    Subclasses ``fastapi.HTTPException`` (not just Starlette's) deliberately:
    FastAPI's body parser re-raises a Starlette ``HTTPException`` unchanged
    but converts any other exception raised while reading the body into a
    generic 400 "There was an error parsing the body" — which would hide a
    413 behind the wrong status and the wrong message. Subclassing
    ``fastapi.HTTPException`` also means ``main.py``'s
    ``@application.exception_handler(HTTPException)`` renders this the same
    ``{"error": ...}`` shape as the declared-length fast path below, whether
    it surfaces from FastAPI's own body parsing or from a route's manual
    ``await request.body()``.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=413, detail=detail)


class MaxBodySizeMiddleware:
    """Rejects a request whose body exceeds its route's byte cap.

    Every route uses ``max_bytes`` except the upload-part route, which uses
    ``part_max_bytes`` instead — ``None`` leaves that route uncapped by this
    middleware, for a caller that only wires the generic limit.

    Two checks, in order:

    1. **Declared length.** A ``Content-Length`` header over the applicable
       limit is rejected outright, before a byte is read — the fast path this
       middleware always had.
    2. **Streaming count.** ``receive`` is wrapped so every ``http.request``
       message's ``body`` bytes are summed as they arrive; once the running
       total exceeds the limit, further reads raise :class:`RequestBodyTooLarge`
       instead of handing the caller more data. This is what catches a chunked
       body (no ``Content-Length`` at all) or one whose declared length
       understates what is actually sent.

    Raising :class:`RequestBodyTooLarge` from the wrapped ``receive`` is only
    half the story, and the half that is *not* enough on its own: every
    Starlette (and therefore every FastAPI) app installs its own
    ``ExceptionMiddleware`` with a default ``HTTPException`` handler, and that
    handler runs *inside* ``self.app(...)`` — before the exception ever has a
    chance to reach this middleware's own ``try``/``except``. For a bare
    ``Starlette()`` app with no customisation, that default handler renders
    the exception as a *plain-text* body, not the ``{"error": ...}`` shape
    every other refusal in this codebase uses; a caller that already wraps
    its own body read in a broad ``except Exception`` (as the upload-part
    route does) or that never installs a handler for the exception at all
    would each behave differently again. Relying on whichever handler happens
    to be installed several layers below this middleware — in test apps this
    module is not allowed to configure — is not a contract this middleware
    can make.

    So ``send`` is wrapped too, and the wrapping does the real work: once the
    byte count has been exceeded, ``tracking_send`` intercepts the *first*
    message any inner layer tries to emit in reaction — whatever it is, a
    bare Starlette app's plain-text default, FastAPI's own JSON ``detail``
    body, a route's hand-rolled conversion — discards it, and substitutes
    this middleware's own canonical ``{"error": ...}`` 413 once. Every message
    after that first substitution is dropped, so a multi-part response body
    from the inner handler cannot trail on after the response this middleware
    already sent. If the genuine response had already started **before** the
    overrun was even detected (streaming out while still reading more of the
    request — not a shape any route here has, but not this middleware's place
    to assume), there is nothing left to safely replace, so the hijack does
    not engage and messages pass through unchanged; whatever exception then
    escapes ``self.app(...)`` is left to propagate, the same as any other
    unhandled error.
    """

    def __init__(self, app: ASGIApp, max_bytes: int, part_max_bytes: int | None = None) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.part_max_bytes = part_max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        is_part_route = _is_part_upload(scope)
        if is_part_route and self.part_max_bytes is None:
            # No part cap configured: this route is uncapped by this
            # middleware, matching the old exemption exactly.
            await self.app(scope, receive, send)
            return

        limit = self.part_max_bytes if is_part_route else self.max_bytes

        declared = next((value for key, value in scope.get("headers", []) if key == b"content-length"), None)
        if declared is not None:
            try:
                declared_bytes = int(declared)
            except ValueError:
                declared_bytes = None
            if declared_bytes is not None and declared_bytes > limit:
                message = _limit_message(limit, is_part_route)
                response = JSONResponse(status_code=413, content={"error": message})
                await response(scope, receive, send)
                return

        received = 0
        # None until the streaming cap is exceeded; then the message this
        # middleware's own response will carry. Its presence is also the
        # switch `tracking_send` uses to decide whether to hijack.
        overrun_detail: str | None = None
        response_started = False
        hijacked = False

        async def limited_receive() -> Message:
            nonlocal received, overrun_detail
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body") or b"")
                if received > limit and overrun_detail is None:
                    overrun_detail = _limit_message(limit, is_part_route)
                    raise RequestBodyTooLarge(overrun_detail)
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started, hijacked
            if overrun_detail is not None and not response_started:
                # See the class docstring: the first message any inner layer
                # tries to send after the overrun is replaced, in full, with
                # this middleware's own JSON 413 — regardless of what that
                # inner layer was trying to say.
                hijacked = True
                response_started = True
                response = JSONResponse(status_code=413, content={"error": overrun_detail})
                await response(scope, receive, send)
                return
            if hijacked:
                # Every message an inner layer tries to send *after* the
                # hijack above (a body chunk following the start line just
                # replaced) is simply dropped, whatever its own `type` —
                # forwarding it would double-send onto a response this
                # middleware already completed.
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except RequestBodyTooLarge:
            # The hijack above is what normally answers the client, and by
            # the time an inner app's exception-handling machinery finishes
            # unwinding it usually believes it already sent a response (it
            # has no way to know `tracking_send` replaced its bytes), so this
            # branch is rarely reached at all. Three cases if it is:
            if hijacked:
                # Already answered above; nothing left to do.
                pass
            elif response_started:
                # A genuine response had already begun *before* the overrun
                # was even detected (see the class docstring); there is
                # nothing safe to replace it with, so this propagates the
                # same as any other exception mid-response would.
                raise
            else:
                # No inner layer attempted to send anything at all — an ASGI
                # app with no exception handling of its own. Same fallback
                # the declared-length path above uses.
                response = JSONResponse(status_code=413, content={"error": overrun_detail})
                await response(scope, receive, send)


def _limit_message(limit: int, is_part_route: bool) -> str:
    mb = limit // (1024 * 1024)
    if is_part_route:
        return f"Upload part exceeds the {mb} MB limit."
    return f"Request body exceeds the {mb} MB limit."


__all__ = ["MaxBodySizeMiddleware", "RequestBodyTooLarge"]
