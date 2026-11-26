"""``GET /api/sessions`` can be narrowed by name, status, creator and workflow.

The filters are one normalised value (``SessionListFilter``) that the route
builds, the page cache keys on and the SQL ``WHERE`` reads. These tests pin
each filter alone, combined with each other and with the existing paging, the
LIKE-wildcard escaping, the validation answers, and that a cached page for one
request is never served for another.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import Settings
from app.core.versioned_cache import VersionedCache
from app.database.change_tracking import install_change_tracking
from app.database.orm import OrmDatabase
from app.domain.session_filters import SESSION_SEARCH_MAX_LENGTH, SessionListFilter
from app.repositories.session_repository import SessionRepository
from app.services.change_feed_service import ChangeFeedService
from app.services.session_service import SessionService
from tests.test_routes import build_test_client
from tests.test_user_admin import GOOD_PASSWORD, MARKER_EMAIL, _activate_marker, _headers, _token


def _owner(user_id: str) -> dict:
    return {"userId": user_id, "username": user_id, "displayName": ""}


def _session(session_id: str, name: str, day: int, **overrides) -> dict:
    payload = {
        "id": session_id,
        "name": name,
        "status": "completed",
        "workflow": "standard",
        "createdAt": f"2026-01-{day:02d}T00:00:00Z",
        "outputs": {},
    }
    payload.update(overrides)
    return payload


async def _seed(repository: SessionRepository, rows: list[dict]) -> None:
    for row in rows:
        await repository.write(row)


def _ids(page: dict) -> list[str]:
    return [row["id"] for row in page["sessions"]]


def _parse(**kwargs) -> SessionListFilter:
    return SessionListFilter.parse(**kwargs)


def _service(tmp_path) -> tuple[SessionService, SessionRepository]:
    database = OrmDatabase(tmp_path / "filters.sqlite3")
    repository = SessionRepository(database)
    return SessionService(Settings(root_dir=tmp_path, backend_root=tmp_path), repository), repository


# --- the value -----------------------------------------------------------------------


def test_parse_normalises_and_validates() -> None:
    parsed = _parse(search="  Smith Case ", statuses=["failed", "COMPLETED", "failed", " "], created_by=" u1 ", workflow="LONG")
    assert parsed.search == "smith case"
    assert parsed.statuses == ("completed", "failed")
    assert parsed.created_by == "u1"
    assert parsed.workflow == "long"
    assert _parse().is_empty and _parse(search="   ", created_by="", workflow="").is_empty
    assert _parse(search="x" * SESSION_SEARCH_MAX_LENGTH).search == "x" * SESSION_SEARCH_MAX_LENGTH
    # Surrounding whitespace does not count toward the cap.
    assert _parse(search=" " + "x" * SESSION_SEARCH_MAX_LENGTH + " ").search
    with pytest.raises(ValueError, match="at most 100"):
        _parse(search="x" * (SESSION_SEARCH_MAX_LENGTH + 1))
    with pytest.raises(ValueError, match="Unknown session status: nope"):
        _parse(statuses=["completed", "nope"])
    with pytest.raises(ValueError, match="Workflow"):
        _parse(workflow="medium")


# --- each filter ---------------------------------------------------------------------


def test_name_search_is_a_case_insensitive_substring_with_literal_wildcards(tmp_path) -> None:
    async def run():
        service, repo = _service(tmp_path)
        await _seed(repo, [
            _session("a", "Alpha Station", 1),
            _session("b", "50% Rule", 2),
            _session("c", "a_b path", 3),
            _session("d", "axb path", 4),
            _session("e", "back\\slash", 5),
            _session("f", "O'Brien \"quoted\"", 6),
            _session("g", "Résumé Étude", 7),
        ])

        async def search(term: str) -> list[str]:
            return _ids(await service.list_page(filters=_parse(search=term)))

        assert await search("alpha") == ["a"]
        assert await search("  STATION  ") == ["a"]
        assert await search("50%") == ["b"]
        assert await search("%") == ["b"]  # a wildcard is a character, not a pattern
        assert await search("a_b") == ["c"]  # not "axb"
        assert await search("_") == ["c"]
        assert await search("\\") == ["e"]
        assert await search("k\\s") == ["e"]
        assert await search("o'brien") == ["f"]
        assert await search('"quoted"') == ["f"]
        assert await search("résumé") == ["g"]
        assert await search("nothing like it") == []
        # An empty search is no filter.
        assert len(_ids(await service.list_page(filters=_parse(search="   ")))) == 7
    asyncio.run(run())


def test_status_is_a_set_of_any_of(tmp_path) -> None:
    async def run():
        service, repo = _service(tmp_path)
        await _seed(repo, [
            _session("a", "a", 1, status="completed"),
            _session("b", "b", 2, status="failed"),
            _session("c", "c", 3, status="processing"),
            _session("d", "d", 4, status="queued"),
        ])
        assert _ids(await service.list_page(filters=_parse(statuses=["failed"]))) == ["b"]
        assert _ids(await service.list_page(filters=_parse(statuses=["queued", "completed"]))) == ["d", "a"]
        assert _ids(await service.list_page(filters=_parse(statuses=["cancelled"]))) == []
    asyncio.run(run())


def test_created_by_matches_the_indexed_creator_and_skips_legacy_rows(tmp_path) -> None:
    async def run():
        service, repo = _service(tmp_path)
        await _seed(repo, [
            _session("mine", "mine", 1, createdBy=_owner("u1")),
            _session("theirs", "theirs", 2, createdBy=_owner("u2")),
            _session("legacy", "legacy", 3),  # no provenance snapshot
        ])
        assert _ids(await service.list_page(filters=_parse(created_by="u1"))) == ["mine"]
        assert _ids(await service.list_page(filters=_parse(created_by="u2"))) == ["theirs"]
        assert _ids(await service.list_page(filters=_parse(created_by="nobody"))) == []
        assert len(_ids(await service.list_page())) == 3
    asyncio.run(run())


def test_workflow_filter_treats_a_session_with_no_workflow_as_standard(tmp_path) -> None:
    async def run():
        service, repo = _service(tmp_path)
        legacy = _session("old", "old", 3)
        del legacy["workflow"]
        await _seed(repo, [_session("std", "std", 1), _session("long", "long", 2, workflow="long"), legacy])
        assert _ids(await service.list_page(filters=_parse(workflow="long"))) == ["long"]
        assert _ids(await service.list_page(filters=_parse(workflow="standard"))) == ["old", "std"]
    asyncio.run(run())


# --- combined, and with the existing paging -------------------------------------------


def test_filters_combine_with_and_with_roots_only_and_parent(tmp_path) -> None:
    async def run():
        service, repo = _service(tmp_path)
        await _seed(repo, [
            _session("r1", "Run one", 1, status="completed", workflow="long", createdBy=_owner("u1")),
            _session("r2", "Run two", 2, status="failed", workflow="long", createdBy=_owner("u1")),
            _session("r3", "Run three", 3, status="completed", workflow="long", createdBy=_owner("u2")),
            _session("c1", "Run one clip", 4, status="completed", parentSessionId="r1", createdBy=_owner("u1")),
        ])
        both = _parse(search="run", statuses=["completed"], created_by="u1", workflow="long")
        assert _ids(await service.list_page(filters=both)) == ["r1"]
        assert _ids(await service.list_page(filters=_parse(search="run", statuses=["completed"], created_by="u1"))) == ["c1", "r1"]
        assert _ids(await service.list_page(filters=_parse(search="run", statuses=["completed"]), roots_only=True)) == ["r3", "r1"]
        assert _ids(await service.list_page(filters=_parse(search="clip"), parent_session_id="r1")) == ["c1"]
        assert _ids(await service.list_page(filters=_parse(search="two"), parent_session_id="r1")) == []
    asyncio.run(run())


def test_a_filtered_listing_pages_with_a_stable_cursor(tmp_path) -> None:
    async def run():
        service, repo = _service(tmp_path)
        rows = [_session(f"m{n}", f"Match {n}", n, createdBy=_owner("u1")) for n in range(1, 8)]
        rows += [_session(f"x{n}", f"Other {n}", n, createdBy=_owner("u1")) for n in range(1, 8)]
        # Same timestamp as m7: the (created_at, id) tiebreak must hold under a filter too.
        rows.append(_session("m7b", "Match 7b", 7, createdBy=_owner("u1")))
        await _seed(repo, rows)
        filters = _parse(search="match", created_by="u1")
        seen: list[str] = []
        cursor = None
        while True:
            page = await service.list_page(limit=3, cursor=cursor, filters=filters)
            seen.extend(_ids(page))
            cursor = page["nextCursor"]
            if cursor is None:
                break
        assert seen == ["m7b", "m7", "m6", "m5", "m4", "m3", "m2", "m1"]
    asyncio.run(run())


def test_name_backfill_still_runs_when_filters_are_present(tmp_path) -> None:
    async def run():
        service, repo = _service(tmp_path)
        await _seed(repo, [
            _session("a", "", 1, status="failed"),
            _session("b", "Named", 2, status="failed"),
            _session("c", "Named", 3, status="completed"),
        ])
        page = await service.list_page(filters=_parse(statuses=["failed"]))
        names = {row["id"]: row["name"] for row in page["sessions"]}
        assert names["a"]  # an unnamed session in the filtered rows got a name
        assert set(_ids(page)) == {"a", "b"}
        # The duplicate lives in the unfiltered set; backfill sees it there too.
        full = await service.list_page(filters=_parse(search="named"))
        assert len({row["name"].lower() for row in full["sessions"]}) == len(full["sessions"])
    asyncio.run(run())


# --- the page cache ------------------------------------------------------------------


def test_a_cached_page_is_never_served_for_a_different_filter(tmp_path) -> None:
    async def run():
        database = OrmDatabase(tmp_path / "cache.sqlite3")
        await database.initialize()
        await install_change_tracking(database.engine)
        changes = ChangeFeedService(database, poll_interval_seconds=0.05)
        await changes.start(push_enabled=False)
        repo = SessionRepository(database)
        cache = VersionedCache()
        service = SessionService(Settings.load(), repo, cache=cache, changes=changes)
        try:
            await _seed(repo, [
                _session("a", "Alpha", 1, status="failed"),
                _session("b", "Beta", 2, status="completed"),
            ])
            assert _ids(await service.list_page()) == ["b", "a"]
            assert _ids(await service.list_page(filters=_parse(statuses=["failed"]))) == ["a"]
            assert _ids(await service.list_page()) == ["b", "a"]
            # Same request spelled differently shares one entry; different ones never do.
            assert cache.hits >= 1  # the repeat of the unfiltered page was a hit, not a rebuild
            assert _ids(await service.list_page(filters=_parse(search="ALPHA"))) == ["a"]
            hits = cache.hits
            assert _ids(await service.list_page(filters=_parse(search=" alpha "))) == ["a"]
            assert cache.hits == hits + 1
            assert _ids(await service.list_page(filters=_parse(search="beta"))) == ["b"]
            assert _ids(await service.list_page(filters=_parse(statuses=["failed", "completed"]))) == ["b", "a"]
            assert _ids(await service.list_page(filters=_parse(statuses=["completed", "failed"]))) == ["b", "a"]
            assert _ids(await service.list_page(filters=_parse(workflow="long"))) == []
        finally:
            await changes.stop()
            await database.shutdown()
    asyncio.run(run())


# --- the route -----------------------------------------------------------------------


def test_the_route_validates_and_resolves_me(tmp_path) -> None:
    client = build_test_client(tmp_path)
    container = client.app.state.container
    admin = _headers(_token(client))
    me = client.get("/api/auth/me", headers=admin).json()
    asyncio.run(_seed(container.sessions.repository, [
        _session("mine", "Mine", 1, createdBy=_owner(me["userId"])),
        _session("theirs", "Theirs", 2, createdBy=_owner("someone-else")),
    ]))

    def listing(query: str):
        return client.get(f"/api/sessions{query}", headers=admin)

    assert [row["id"] for row in listing("?createdBy=me").json()["sessions"]] == ["mine"]
    assert [row["id"] for row in listing("?createdBy=someone-else").json()["sessions"]] == ["theirs"]
    assert [row["id"] for row in listing("?q=THEIRS&status=completed&workflow=standard").json()["sessions"]] == ["theirs"]
    assert len(listing("?q=").json()["sessions"]) == 2

    assert listing("?status=completed&status=nope").status_code == 422
    assert listing("?workflow=medium").status_code == 422
    assert listing("?q=" + "x" * 101).status_code == 422
    assert listing("?q=" + "x" * 100).status_code == 200


def test_created_by_me_for_an_account_with_no_sessions_is_an_empty_page(tmp_path) -> None:
    client = build_test_client(tmp_path)
    admin_token = _token(client)
    _activate_marker(client, admin_token)
    marker = _headers(_token(client, MARKER_EMAIL, GOOD_PASSWORD))
    asyncio.run(_seed(client.app.state.container.sessions.repository, [_session("a", "A", 1)]))
    page = client.get("/api/sessions?createdBy=me", headers=marker).json()
    assert page == {"sessions": [], "nextCursor": None}
    assert len(client.get("/api/sessions", headers=marker).json()["sessions"]) == 1
