"""The real application, imported safely, and its routes as FastAPI serves them.

Three test modules inspect ``app.main.app`` without starting it — the route
guards (``test_admin_routes_are_guarded.py``, ``test_session_routes_are_guarded.py``)
walk its routes, and ``test_error_response_shape.py`` calls its exception
handlers. Two things about that are easy to get wrong, so they live here once:

* Importing ``app.main`` runs its logging setup, which stops the ``app``
  logger propagating to the root — and pytest's ``caplog`` listens on the
  root. :data:`app` is imported with that change saved and restored, so tests
  that run after these modules still see the log lines they assert on.

* ``app.routes`` is not a flat list of endpoints. Since FastAPI 0.140 an
  ``include_router`` call keeps the included router as one nested entry
  instead of copying its routes up, so a walk over ``app.routes`` finds the
  ``/media`` mounts and nothing under ``/api`` — and a guard test built on it
  passes vacuously. :func:`api_routes` asks FastAPI for the *effective*
  routes instead (``fastapi.routing.iter_route_contexts``, the same walk the
  OpenAPI generator makes): each carries its full path and a ``dependant``
  that already includes every dependency declared on the routers above it,
  which is exactly what a guard test needs to check.
"""

from __future__ import annotations

import logging

from fastapi.routing import APIRoute, RouteContext, iter_route_contexts

_app_logger = logging.getLogger("app")
_saved_logging = (_app_logger.propagate, list(_app_logger.handlers), _app_logger.level)
from app.main import app  # noqa: E402

_app_logger.propagate, _app_logger.handlers[:], _ = _saved_logging
_app_logger.setLevel(_saved_logging[2])

__all__ = ["app", "api_routes"]


def api_routes() -> list[RouteContext]:
    """Every HTTP endpoint the application serves, with its effective path,
    methods and ``dependant`` — nested routers flattened, router-level
    dependencies included. Mounts (``/media``, the SPA) are not endpoints and
    are left out."""
    return [context for context in iter_route_contexts(app.routes) if isinstance(context.original_route, APIRoute)]
