"""Default ``Cache-Control`` for everything under ``/api`` and ``/media``.

Complements ``app/core/security_headers.py``: that module hardens what a
browser does with a response, this one hardens what a CDN in front of it does.

The gap this closes is specific to putting this app behind Cloudflare (or any
edge cache) rather than a bare reverse proxy. Cloudflare caches a response by
file extension *by default* even when the origin sends no ``Cache-Control`` at
all — ``.mp4``, ``.mp3`` and ``.pdf`` are all in its default cacheable-
extensions list — and, absent an origin header to size the TTL from, it falls
back to its own default edge TTL (on the order of hours). The ``/media/*``
routes (``app/main.py``) are Starlette ``StaticFiles`` mounts: they send
``ETag`` / ``Last-Modified`` for conditional requests but never set
``Cache-Control`` themselves, so every one of them was relying entirely on the
edge's own judgement.

That would be a merely wasteful default for genuinely public assets, but media
here is never that: every URL carries a short-lived ``?ticket=`` query
parameter (``STREAM_TICKET_TTL_SECONDS``, see "Per-session SSE" /
"Authentication and accounts" in CLAUDE.md) rather than a durable credential,
*and* the query string is part of Cloudflare's default cache key. So a session
video fetched once would sit at the edge, keyed by that now-expired ticket,
and stay retrievable by that exact URL for as long as Cloudflare's TTL runs —
long after the ticket that was supposed to gate it has expired. ``/api``
responses carry no artefact bytes but are exactly as capable of holding a
result behind a stale credential, so the same rule covers both prefixes.

``private, no-store`` is deliberately the strongest instruction in the
Cache-Control vocabulary: it tells both the browser and every intermediary
never to persist the response at all, which is the only safe default for a
family of URLs whose authorization is a value *inside* the URL itself. A route
with a real reason to cache something (the frontend's own hashed assets in
``app/api/frontend.py``, the SSE endpoints' ``no-cache, no-transform`` in
``app/api/routes/events.py`` and ``sessions.py``) sets its own header, and the
security-headers middleware applies this one with ``setdefault`` — see
``app.main.add_security_headers`` — so those routes are left untouched.
"""

from __future__ import annotations

NO_STORE_CACHE_CONTROL = "private, no-store"


def default_cache_control(path: str) -> str | None:
    """The ``Cache-Control`` a response for ``path`` should default to.

    ``/api`` and ``/media`` (any depth under either) get the no-store
    instruction described above; everything else (the frontend mount, or a
    deployment that serves it separately) is left alone — ``None`` means "do
    not set a default", not "cache forever".
    """
    if path == "/api" or path.startswith("/api/") or path.startswith("/media/"):
        return NO_STORE_CACHE_CONTROL
    return None


__all__ = ["NO_STORE_CACHE_CONTROL", "default_cache_control"]
