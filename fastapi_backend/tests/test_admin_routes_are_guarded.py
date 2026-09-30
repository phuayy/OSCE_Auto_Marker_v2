"""Every route under ``/api/admin/`` carries the administrator gate.

The gate is declared once, on the router (``APIRouter(dependencies=[...])``),
so a handler added to ``routes/users.py`` inherits it. What that does not
cover is a *second* admin router, or a handler mounted elsewhere under the
same prefix, so this walks the real application's routes and checks each one
by identity — the same fail-closed idea as ``test_status_vocabulary.py``.
"""

from __future__ import annotations

from fastapi.routing import RouteContext

from app.api.dependencies import require_admin
from tests.fixtures.app_routes import api_routes


def _admin_routes() -> list[RouteContext]:
    return [route for route in api_routes() if route.path.startswith("/api/admin/")]


def _has_admin_gate(route: RouteContext) -> bool:
    return any(dependency.call is require_admin for dependency in route.dependant.dependencies)


def test_there_are_admin_routes_to_guard() -> None:
    assert _admin_routes(), "the account-administration router should be mounted under /api/admin/"


def test_every_admin_route_depends_on_require_admin() -> None:
    unguarded = [f"{sorted(route.methods)} {route.path}" for route in _admin_routes() if not _has_admin_gate(route)]
    assert unguarded == [], "routes under /api/admin/ without require_admin:\n  " + "\n  ".join(unguarded)


def test_the_gate_names_the_role_it_admits() -> None:
    assert require_admin.required_roles == frozenset({"admin"})
