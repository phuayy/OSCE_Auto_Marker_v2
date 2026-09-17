"""Every route under ``/api/admin/`` carries the administrator gate.

The gate is declared once, on the router (``APIRouter(dependencies=[...])``),
so a handler added to ``routes/users.py`` inherits it. What that does not
cover is a *second* admin router, or a handler mounted elsewhere under the
same prefix, so this walks the real application's routes and checks each one
by identity — the same fail-closed idea as ``test_status_vocabulary.py``.
"""

from __future__ import annotations

import logging

from fastapi.routing import APIRoute

from app.api.dependencies import require_admin

# Importing the application module runs its logging setup, which stops the
# ``app`` logger propagating to the root — and pytest's ``caplog`` listens on
# the root. Restore what the import changed so the tests that run after this
# module still see the log lines they assert on.
_app_logger = logging.getLogger("app")
_saved_logging = (_app_logger.propagate, list(_app_logger.handlers), _app_logger.level)
from app.main import app  # noqa: E402

_app_logger.propagate, _app_logger.handlers[:], _ = _saved_logging
_app_logger.setLevel(_saved_logging[2])


def _admin_routes() -> list[APIRoute]:
    return [route for route in app.routes if isinstance(route, APIRoute) and route.path.startswith("/api/admin/")]


def _has_admin_gate(route: APIRoute) -> bool:
    return any(dependency.call is require_admin for dependency in route.dependant.dependencies)


def test_there_are_admin_routes_to_guard() -> None:
    assert _admin_routes(), "the account-administration router should be mounted under /api/admin/"


def test_every_admin_route_depends_on_require_admin() -> None:
    unguarded = [f"{sorted(route.methods)} {route.path}" for route in _admin_routes() if not _has_admin_gate(route)]
    assert unguarded == [], "routes under /api/admin/ without require_admin:\n  " + "\n  ".join(unguarded)


def test_the_gate_names_the_role_it_admits() -> None:
    assert require_admin.required_roles == frozenset({"admin"})
