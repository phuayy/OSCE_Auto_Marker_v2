"""Every mutating route under /api/sessions, /api/jobs and /api/uploads
carries an ownership gate.

Sibling of test_admin_routes_are_guarded.py: that one checks "is this an
admin", this one checks "did you create this" — require_session_owner,
require_job_owner and require_upload_owner (app/api/dependencies.py), applied
as a route dependency so a handler added to one of these routers without
remembering the gate fails this test instead of shipping unprotected. Reads
(GET) are deliberately excluded — every marker may still see every session —
and POST /api/uploads/initiate is an explicit, commented exception: it
creates the record the other routes gate, so there is nothing yet to own.
"""

from __future__ import annotations

from fastapi.routing import RouteContext

from app.api.dependencies import require_job_owner, require_session_owner, require_upload_owner
from tests.fixtures.app_routes import api_routes

_GUARDED_PREFIXES = ("/api/sessions/", "/api/jobs/", "/api/uploads/")
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_OWNERSHIP_GATES = (require_session_owner, require_job_owner, require_upload_owner)

# Creates the record every other route in this module guards; there is no
# owner yet to check. Every other mutating route under the three prefixes
# above must carry one of _OWNERSHIP_GATES.
_ALLOWLIST = {("POST", "/api/uploads/initiate")}


def _mutating_routes() -> list[RouteContext]:
    return [
        route
        for route in api_routes()
        if route.path.startswith(_GUARDED_PREFIXES) and route.methods & _MUTATING_METHODS
    ]


def _has_ownership_gate(route: RouteContext) -> bool:
    return any(dependency.call in _OWNERSHIP_GATES for dependency in route.dependant.dependencies)


def test_there_are_mutating_session_routes_to_guard() -> None:
    assert _mutating_routes(), "expected mutating routes under /api/sessions, /api/jobs or /api/uploads"


def test_every_mutating_route_is_owner_gated_or_allowlisted() -> None:
    unguarded = [
        f"{sorted(route.methods)} {route.path}"
        for route in _mutating_routes()
        if not _has_ownership_gate(route)
        and not any((method, route.path) in _ALLOWLIST for method in route.methods)
    ]
    assert unguarded == [], "mutating routes with no ownership gate:\n  " + "\n  ".join(unguarded)


def test_the_allowlist_names_only_routes_that_actually_exist() -> None:
    real = {(method, route.path) for route in _mutating_routes() for method in route.methods}
    stale = _ALLOWLIST - real
    assert stale == set(), f"allowlist entries that no longer match a route: {stale}"
