"""Baseline HTTP response headers, applied to every response regardless of
route or outcome (a 401 from the auth middleware included).

Complements the auth middleware in ``app.main``: that one decides *who* may
reach a route, this one hardens what the browser does with what it gets back.
None of this replaces authentication or input validation — it is defense in
depth against the class of bug that gets through anyway (a reflected value
rendered unescaped somewhere, a click-jacking iframe, a sniffed content type).
"""

from __future__ import annotations

from app.core.config import Settings

# This app is a same-origin SPA: every script, style, image, font and API/media
# call it makes is served by itself (see CLAUDE.md "One API client" / "Serve
# the built frontend"). No third-party origin belongs in any directive here
# unless the frontend starts loading one.

# index.html runs exactly one inline classic script before anything else: the
# theme boot, which adds the dark class in the same parse step as the document
# (index.html, mirroring src/lib/theme.js). It is inline for the reason it
# exists — an external file costs a request before the first paint, and a
# `type="module"` one is deferred past it, either of which is the white flash
# the script is there to prevent. So it is named here instead: a hash keeps
# `script-src` strict, admitting this exact script while still refusing
# `'unsafe-inline'` and so anything injected.
#
# Vite copies the script into dist/index.html byte for byte, so this covers
# the built app an operator actually serves (SERVE_FRONTEND=true), not just
# the source. tests/test_security_headers.py recomputes the digest from
# index.html, so editing either side fails a test rather than a browser.
THEME_BOOT_SCRIPT_HASH = "sha256-xlM6V+tRQErr140pAz50kQrp/Yb2W8bAPwTwDzpb7P8="

DEFAULT_CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        f"script-src 'self' '{THEME_BOOT_SCRIPT_HASH}'",
        # framer-motion (a vendor chunk of this app, see CLAUDE.md "Code
        # splitting") animates via inline `style` attributes; refusing that
        # would break every transition for a class of injection (styling)
        # that script-src's unconditional refusal already covers the far
        # more dangerous half of.
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "media-src 'self' blob:",
        "connect-src 'self'",
        "worker-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)


def build_content_security_policy(settings: Settings) -> str:
    """The CSP to send, honouring an operator override.

    ``CONTENT_SECURITY_POLICY`` is an escape hatch for the day the frontend
    legitimately needs a third-party origin (a font CDN, an embed) — a config
    change rather than a code change.
    """
    return settings.content_security_policy_override or DEFAULT_CONTENT_SECURITY_POLICY


def apply_security_headers(headers: object, settings: Settings) -> None:
    """Set the baseline headers on a mutable header mapping.

    ``headers`` is a Starlette ``Response.headers`` (a ``MutableHeaders``,
    supporting ``setdefault``) or anything with the same contract — kept
    duck-typed so this stays trivially unit-testable with a plain dict.
    ``setdefault`` throughout: a route with a deliberate reason to set one of
    these itself (none does today) is not silently overridden.
    """
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    headers.setdefault("Content-Security-Policy", build_content_security_policy(settings))
    if settings.hsts_enabled:
        # Browsers only ever honour this over an HTTPS connection (RFC 6797);
        # sending it on a plain-HTTP response is inert, not dangerous. The
        # opt-in guard (hsts_enabled) exists so an operator cannot turn this
        # on in front of the on-prem, TLS-less deployment shape this app
        # documents and find every browser that ever loaded the page locked
        # out of plain HTTP for max-age.
        headers.setdefault(
            "Strict-Transport-Security",
            f"max-age={max(0, settings.hsts_max_age_seconds)}; includeSubDomains",
        )


__all__ = [
    "DEFAULT_CONTENT_SECURITY_POLICY",
    "THEME_BOOT_SCRIPT_HASH",
    "apply_security_headers",
    "build_content_security_policy",
]
