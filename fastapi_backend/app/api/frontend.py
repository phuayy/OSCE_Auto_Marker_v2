"""Serve the built frontend from the API process.

A single-box deployment — one VM, local storage, other devices uploading over
the network — needs the browser to load the app from somewhere. Vite serves
the sources in development and proxies ``/api`` and ``/media`` to this API; in
production the equivalent is ``npm run build`` and a static server in front of
``dist/``. This module makes that server optional: with ``SERVE_FRONTEND=true``
the API mounts ``dist/`` itself, at the same origin as the API, which is also
what keeps CORS out of the picture and lets the relative ``/api`` and
``/media`` URLs the frontend already uses resolve unchanged.

Three things a plain ``StaticFiles`` would get wrong for a Vite build:

* **Hashed assets are immutable, ``index.html`` is not.** Every file under
  ``assets/`` carries its content hash in its name, so a browser may keep it
  for a year; ``index.html`` is what names the current hashes and must be
  revalidated on every load, or a returning browser keeps asking a new
  deploy for chunks it no longer has (the failure ``lazyRoute.jsx``'s chunk
  boundary exists to catch).
* **A path the build has no file for is a navigation, not a miss.** The app
  routes on the URL hash, so its deep links are all ``/#/...`` and only ``/``
  is ever requested — but a proxy that rewrites, a bookmark, or a future
  path-based route would otherwise answer 404 with the API's JSON body.
  Anything without a file extension answers ``index.html`` and lets the app
  boot; an asset that is missing still 404s, so a stale chunk request fails
  loudly rather than parsing HTML as JavaScript.
* **The mount must come last.** Starlette matches routes in order; ``/api``
  and ``/media`` are registered first so this catch-all can only see what
  they did not claim.

The auth middleware already lets non-``/api``, non-``/media`` paths through
(``authorize_request``), so the login page loads without a token.
"""

from __future__ import annotations

import logging
from pathlib import Path, PurePath, PurePosixPath

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from app.core.config import Settings

logger = logging.getLogger(__name__)

INDEX_FILE = "index.html"
# Vite's default output directory for content-hashed chunks and styles.
IMMUTABLE_ASSET_PREFIX = "assets/"
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
# "Always revalidate" — not "never store". The browser keeps the entry and asks
# whether it changed (ETag / Last-Modified from FileResponse), so a reload
# costs one conditional request rather than a re-download.
REVALIDATE_CACHE_CONTROL = "no-cache"


def relative_url_path(path: str) -> str:
    """The path ``StaticFiles`` hands ``get_response``, as URL segments.

    Starlette builds it with ``os.path.join`` + ``normpath``, so on Windows it
    arrives with backslashes and the root arrives as ``"."``; the rules below
    are about URLs and must not depend on either.
    """
    normalised = PurePath(path).as_posix().strip("/")
    return "" if normalised == "." else normalised


def is_navigation_path(path: str) -> bool:
    """A request path that should boot the app rather than name a file.

    A file the build produced has an extension (``.js``, ``.css``, ``.svg``,
    ``.woff2``); a route does not. The asset directory is excluded outright so
    a chunk a new deploy dropped can never come back as HTML.
    """
    normalised = relative_url_path(path)
    if not normalised:
        return True
    if normalised.startswith(IMMUTABLE_ASSET_PREFIX):
        return False
    return PurePosixPath(normalised).suffix == ""


def cache_control_for(path: str, *, served_index: bool) -> str | None:
    """The ``Cache-Control`` a served file should carry, or None to leave the
    default (``public/`` files such as a favicon are neither hashed nor the
    document, so the browser's heuristics are fine for them)."""
    if served_index:
        return REVALIDATE_CACHE_CONTROL
    if relative_url_path(path).startswith(IMMUTABLE_ASSET_PREFIX):
        return IMMUTABLE_CACHE_CONTROL
    return None


class SinglePageAppFiles(StaticFiles):
    """``dist/`` as Vite builds it: hashed assets cached for a year,
    ``index.html`` revalidated every time, unknown routes answered with
    ``index.html`` so the app boots and routes itself."""

    def __init__(self, directory: Path) -> None:
        # check_dir=False: a missing build is a startup warning
        # (Settings._frontend_warnings), not an import-time crash of the API.
        super().__init__(directory=str(directory), html=True, check_dir=False)

    async def check_config(self) -> None:
        # Starlette's first-request check raises when the directory is missing
        # — SERVE_FRONTEND on, `npm run build` not yet run. Skipping it turns
        # that into 404s: the API stays up, the startup warning names the fix,
        # and a build that lands later is served without a restart.
        if not Path(self.directory).is_dir():
            return
        await super().check_config()

    async def get_response(self, path: str, scope: Scope) -> Response:
        served_index = relative_url_path(path) in {"", INDEX_FILE}
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or not is_navigation_path(path):
                raise
            response = await super().get_response(INDEX_FILE, scope)
            served_index = True
        cache_control = cache_control_for(path, served_index=served_index)
        if cache_control is not None:
            response.headers["Cache-Control"] = cache_control
        return response


def mount_frontend(app: FastAPI, settings: Settings) -> bool:
    """Mount the built frontend at ``/`` when ``SERVE_FRONTEND`` asks for it.

    Call after every router and media mount — the catch-all must come last.
    Returns whether a mount was added, so a caller can log or test it.
    """
    if not settings.serve_frontend:
        return False
    dist_dir = settings.frontend_dist_dir
    app.mount("/", SinglePageAppFiles(dist_dir), name="frontend")
    if settings.frontend_index_path.is_file():
        logger.info("Serving the built frontend from %s", dist_dir)
    else:
        # The Settings warning says the same at boot; this one is here for a
        # process that mounted without going through startup (tests, tooling).
        logger.warning("SERVE_FRONTEND is enabled but %s has no index.html", dist_dir)
    return True
