"""``POST /api/sessions/bulk`` — one action over many sessions, each judged alone.

The route has no ``{session_id}`` for ``require_session_owner`` to read, so
the creator-or-admin gate runs per item; the expensive-operation budget is
spent once per request and only by an action that spends it; an item's refusal
is that item's result and never stops the rest.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.exceptions import AppError
from app.core.rate_limit import FixedWindowRateLimiter
from app.services.session_bulk_service import BULK_SESSION_ACTIONS, BulkAction, SessionBulkService
from tests.test_routes import build_test_client
from tests.test_user_admin import GOOD_PASSWORD, MARKER_EMAIL, _activate_marker, _headers, _token


def _session(session_id: str, **overrides) -> dict:
    payload = {"id": session_id, "name": session_id, "status": "completed", "createdAt": "2026-01-01T00:00:00Z", "outputs": {}}
    payload.update(overrides)
    return payload


def _owner(user_id: str) -> dict:
    return {"userId": user_id, "username": user_id, "displayName": ""}


class _World:
    """A client, an admin, and a marker, with the maintenance calls recorded."""

    def __init__(self, tmp_path, monkeypatch, budget: int = 10) -> None:
        self.client = build_test_client(tmp_path)
        self.container = self.client.app.state.container
        self.container.expensive_operation_rate_limiter = FixedWindowRateLimiter(max_attempts=budget, window_seconds=3600)
        self.admin = _headers(_token(self.client))
        _activate_marker(self.client, _token(self.client))
        self.marker = _headers(_token(self.client, MARKER_EMAIL, GOOD_PASSWORD))
        self.marker_id = self.client.get("/api/auth/me", headers=self.marker).json()["userId"]
        self.reruns: list[str] = []

        async def rerun(session_id: str) -> dict:
            self.reruns.append(session_id)
            return {"ok": True}

        monkeypatch.setattr(self.container.session_maintenance, "rerun_session", rerun)

    def seed(self, *rows: dict) -> None:
        for row in rows:
            asyncio.run(self.container.sessions.write(row))

    def post(self, action, ids, headers=None, **extra):
        return self.client.post("/api/sessions/bulk", headers=headers or self.marker, json={"action": action, "sessionIds": ids, **extra})

    def exists(self, session_id: str) -> bool:
        try:
            asyncio.run(self.container.sessions.read(session_id))
            return True
        except FileNotFoundError:
            return False


@pytest.fixture
def world(tmp_path, monkeypatch) -> _World:
    return _World(tmp_path, monkeypatch)


def _by_id(response) -> dict[str, dict]:
    return {item["sessionId"]: item for item in response.json()["results"]}


# --- per item ------------------------------------------------------------------------


def test_each_item_is_judged_on_its_own_and_in_request_order(world: _World) -> None:
    world.seed(
        _session("mine", createdBy=_owner(world.marker_id)),
        _session("theirs", createdBy=_owner("someone-else")),
        _session("legacy"),  # no provenance: mutable by anyone, as everywhere
    )
    response = world.post("rerun", ["theirs", "missing", "mine", "legacy"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["sessionId"] for item in body["results"]] == ["theirs", "missing", "mine", "legacy"]
    results = _by_id(response)
    assert results["theirs"] == {
        "sessionId": "theirs", "ok": False, "status": 403, "code": "forbidden",
        "error": "Only the session's creator or an administrator may change it.",
    }
    assert results["missing"] == {"sessionId": "missing", "ok": False, "status": 404, "code": "not_found", "error": "Session not found."}
    assert results["mine"] == {"sessionId": "mine", "ok": True, "status": 200}
    assert results["legacy"]["ok"] is True
    assert body["summary"] == {"ok": 2, "failed": 2}
    # The refused and missing ids were never acted on; the others were, in order.
    assert world.reruns == ["mine", "legacy"]


def test_an_administrator_may_act_on_anyone_s_sessions(world: _World) -> None:
    world.seed(_session("theirs", createdBy=_owner("someone-else")))
    response = world.post("delete", ["theirs"], headers=world.admin)
    assert response.json()["summary"] == {"ok": 1, "failed": 0}
    assert not world.exists("theirs")


def test_a_marker_cannot_delete_someone_else_s_session(world: _World) -> None:
    world.seed(_session("theirs", createdBy=_owner("someone-else")), _session("mine", createdBy=_owner(world.marker_id)))
    response = world.post("delete", ["theirs", "mine"])
    assert _by_id(response)["theirs"]["code"] == "forbidden"
    assert world.exists("theirs") and not world.exists("mine")


def test_rerun_of_an_in_flight_session_is_a_per_item_conflict(tmp_path) -> None:
    # The real maintenance service: no stubbing of rerun_session here.
    client = build_test_client(tmp_path)
    container = client.app.state.container
    admin = _headers(_token(client))
    for row in (_session("busy", status="processing"), _session("queued", status="queued")):
        asyncio.run(container.sessions.write(row))
    results = _by_id(client.post("/api/sessions/bulk", headers=admin, json={"action": "rerun", "sessionIds": ["busy", "queued"]}))
    for item in results.values():
        assert (item["ok"], item["status"], item["code"]) == (False, 409, "conflict")
        assert "already being processed" in item["error"]


def test_an_unexpected_failure_is_a_generic_item_result_and_the_rest_continue(world: _World, monkeypatch) -> None:
    world.seed(_session("boom"), _session("fine"))

    async def rerun(session_id: str) -> dict:
        if session_id == "boom":
            raise RuntimeError("secret path /srv/x leaked")
        world.reruns.append(session_id)
        return {}

    monkeypatch.setattr(world.container.session_maintenance, "rerun_session", rerun)
    results = _by_id(world.post("rerun", ["boom", "fine"]))
    assert results["boom"]["status"] == 500 and results["boom"]["code"] == "failed"
    assert "secret" not in results["boom"]["error"]
    assert results["fine"]["ok"] is True and world.reruns == ["fine"]


def test_an_app_error_keeps_its_status_and_message(world: _World, monkeypatch) -> None:
    world.seed(_session("a"))

    async def rerun(session_id: str) -> dict:
        raise AppError("Source video is missing.", status_code=422)

    monkeypatch.setattr(world.container.session_maintenance, "rerun_session", rerun)
    item = _by_id(world.post("rerun", ["a"]))["a"]
    assert (item["status"], item["code"], item["error"]) == (422, "rejected", "Source video is missing.")


# --- delete --------------------------------------------------------------------------


def test_deleting_a_recording_removes_its_clip_children_like_the_single_delete(world: _World) -> None:
    world.seed(
        _session("parent", workflow="long", createdBy=_owner(world.marker_id)),
        _session("child-1", parentSessionId="parent", createdBy=_owner(world.marker_id)),
        _session("child-2", parentSessionId="parent", createdBy=_owner(world.marker_id)),
        _session("other"),
    )
    response = world.post("delete", ["parent"])
    assert response.json()["summary"] == {"ok": 1, "failed": 0}
    assert [world.exists(name) for name in ("parent", "child-1", "child-2", "other")] == [False, False, False, True]


def _recording_with_clips(world: _World) -> None:
    owner = _owner(world.marker_id)
    world.seed(
        _session("parent", workflow="long", createdBy=owner),
        _session("child-1", parentSessionId="parent", createdBy=owner),
        _session("child-2", parentSessionId="parent", createdBy=owner),
    )


def test_a_clip_ticked_after_its_recording_is_covered_not_failed(world: _World) -> None:
    _recording_with_clips(world)
    response = world.post("delete", ["parent", "child-1", "ghost", "child-2"])
    results = _by_id(response)
    note = "Deleted with its recording."
    assert results["child-1"] == {"sessionId": "child-1", "ok": True, "status": 200, "note": note}
    assert results["child-2"]["note"] == note and results["parent"].get("note") is None
    assert results["ghost"]["code"] == "not_found"  # a genuinely unknown id still fails
    assert response.json()["summary"] == {"ok": 3, "failed": 1}


def test_a_clip_ticked_before_its_recording_is_a_plain_success(world: _World) -> None:
    _recording_with_clips(world)
    response = world.post("delete", ["child-1", "parent", "child-2", "ghost"])
    results = _by_id(response)
    assert results["child-1"] == {"sessionId": "child-1", "ok": True, "status": 200}
    assert results["child-2"]["note"] == "Deleted with its recording."
    assert results["ghost"]["ok"] is False
    assert response.json()["summary"] == {"ok": 3, "failed": 1}
    assert not any(world.exists(name) for name in ("parent", "child-1", "child-2"))


def test_a_missing_clip_is_not_covered_unless_its_recording_was_deleted_here(world: _World) -> None:
    world.seed(_session("parent", workflow="long", createdBy=_owner("someone-else")))
    # Refused parent (403): nothing was deleted, so an unknown id stays a 404.
    results = _by_id(world.post("delete", ["parent", "ghost"]))
    assert results["parent"]["code"] == "forbidden" and results["ghost"]["code"] == "not_found"


# --- the budget ----------------------------------------------------------------------


def test_rerun_spends_the_budget_once_per_request_not_per_item(tmp_path, monkeypatch) -> None:
    world = _World(tmp_path, monkeypatch, budget=10)
    ids = [f"s{n}" for n in range(11)]
    world.seed(*(_session(name) for name in ids))
    first = world.post("rerun", ids)
    assert first.status_code == 200 and first.json()["summary"] == {"ok": 11, "failed": 0}
    # One unit spent so far: nine more single requests fit, the tenth does not.
    for _ in range(9):
        assert world.post("rerun", ["s0"]).status_code == 200
    refused = world.post("rerun", ["s0"])
    assert refused.status_code == 429
    assert len(world.reruns) == 11 + 9


def test_delete_spends_nothing_and_a_rejected_request_spends_nothing(tmp_path, monkeypatch) -> None:
    world = _World(tmp_path, monkeypatch, budget=1)
    world.seed(*(_session(f"d{n}") for n in range(5)))
    for n in range(5):
        assert world.post("delete", [f"d{n}"]).status_code == 200
    # Invalid requests are refused before the limiter is consulted.
    assert world.post("rerun", []).status_code == 422
    assert world.post("explode", ["x"]).status_code == 422
    world.seed(_session("ok"))
    assert world.post("rerun", ["ok"]).status_code == 200
    assert world.post("rerun", ["ok"]).status_code == 429


# --- validation ----------------------------------------------------------------------


def test_the_request_is_validated_before_anything_runs(world: _World) -> None:
    world.seed(_session("a"))
    ids = [f"s{n}" for n in range(101)]
    assert world.post("rerun", []).status_code == 422
    assert world.post("rerun", ids).status_code == 422
    inclusive = world.post("rerun", ids[:100])  # the cap is inclusive; none of these exist
    assert inclusive.status_code == 200 and inclusive.json()["summary"] == {"ok": 0, "failed": 100}
    assert world.post("rerun", ["a", "a"]).status_code == 422
    assert world.post("rerun", ["a", " "]).status_code == 422
    assert world.post("explode", ["a"]).status_code == 422
    assert world.post("rerun", ["a"], options={"branches": ["content"]}).status_code == 422  # takes no options
    # A malformed body is the app-wide 400, as for every other route.
    assert world.client.post("/api/sessions/bulk", headers=world.marker, json={"action": "rerun"}).status_code == 400
    assert world.client.post("/api/sessions/bulk", headers=world.marker, json={"action": "rerun", "sessionIds": "a"}).status_code == 400
    assert world.client.post("/api/sessions/bulk").status_code == 401
    assert world.reruns == []  # no rejected request ran anything


def test_options_are_validated_per_action(monkeypatch) -> None:
    from pydantic import BaseModel, Field

    class Options(BaseModel):
        branches: list[str] = Field(min_length=1)

    async def handler(_service, _session_id, _options):
        return None

    monkeypatch.setitem(BULK_SESSION_ACTIONS, "demo", BulkAction(handler, charges_budget=True, options_model=Options))
    plan = SessionBulkService.prepare("demo", ["a"], {"branches": ["content"]})
    assert plan.options == Options(branches=["content"]) and plan.charges_budget
    with pytest.raises(ValueError, match="Invalid options for 'demo'"):
        SessionBulkService.prepare("demo", ["a"], {"branches": []})
    with pytest.raises(ValueError, match="Invalid options for 'demo'"):
        SessionBulkService.prepare("demo", ["a"], None)


# --- re-score ------------------------------------------------------------------------


def _rescore_world(tmp_path, monkeypatch, budget: int = 10) -> _World:
    from tests.test_session_rescore import _hold_jobs, _scored_session

    world = _World(tmp_path, monkeypatch, budget=budget)
    _hold_jobs(world.container)
    asyncio.run(_scored_session(world.container, "ok", createdBy=_owner(world.marker_id)))
    world.seed(
        _session("busy", status="processing", createdBy=_owner(world.marker_id)),
        _session("bare", createdBy=_owner(world.marker_id)),  # completed, no transcript on disk
        _session("theirs", createdBy=_owner("someone-else")),
    )
    return world


def test_rescore_reports_each_item_with_the_rescore_code(tmp_path, monkeypatch) -> None:
    world = _rescore_world(tmp_path, monkeypatch)
    response = world.post("rescore", ["ok", "busy", "bare", "theirs", "missing"], options={"branches": ["content"]})
    assert response.status_code == 200, response.text
    results = _by_id(response)
    assert (results["ok"]["ok"], results["ok"]["status"]) == (True, 200) and "code" not in results["ok"]
    assert (results["busy"]["status"], results["busy"]["code"]) == (409, "in_flight")
    assert (results["bare"]["status"], results["bare"]["code"]) == (409, "no_transcript")
    assert (results["theirs"]["status"], results["theirs"]["code"]) == (403, "forbidden")
    assert (results["missing"]["status"], results["missing"]["code"]) == (404, "not_found")
    assert response.json()["summary"] == {"ok": 1, "failed": 4}
    # Only the eligible session was queued, and only for the chosen branch.
    jobs = asyncio.run(world.container.jobs.list_jobs("ok"))
    assert [job["payload"]["branches"] for job in jobs] == [["content"]]
    assert asyncio.run(world.container.jobs.list_jobs("busy")) == []


@pytest.mark.parametrize(
    "options",
    [None, {}, {"branches": []}, {"branches": ["content", "content"]}, {"branches": ["audio"]}, {"branches": "content"}],
)
def test_rescore_with_missing_or_invalid_branches_is_a_422_that_runs_nothing(tmp_path, monkeypatch, options) -> None:
    world = _rescore_world(tmp_path, monkeypatch, budget=1)
    response = world.post("rescore", ["ok"], **({} if options is None else {"options": options}))
    assert response.status_code == 422, response.text
    assert asyncio.run(world.container.jobs.list_jobs("ok")) == []
    # Refused before the budget was consulted: the one unit is still there.
    assert world.post("rescore", ["ok"], options={"branches": ["content"]}).status_code == 200


def test_rescore_spends_the_budget_once_per_request(tmp_path, monkeypatch) -> None:
    from tests.test_session_rescore import _scored_session

    world = _rescore_world(tmp_path, monkeypatch, budget=1)
    for name in ("two", "three"):
        asyncio.run(_scored_session(world.container, name, createdBy=_owner(world.marker_id)))
    options = {"branches": ["content", "communication"]}
    first = world.post("rescore", ["ok", "two", "three"], options=options)
    assert first.status_code == 200 and first.json()["summary"] == {"ok": 3, "failed": 0}
    assert world.post("rescore", ["ok"], options=options).status_code == 429
