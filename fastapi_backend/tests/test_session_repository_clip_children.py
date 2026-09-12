"""Finding the child session that assesses one clip.

"One clip, one child" is only enforceable if the store can answer "does this
clip already have a child?" cheaply — which means filtering on the indexed
parent column *and* on the clip id inside ``clip_source``, in the database,
rather than loading every child's payload. These run against real SQLite
because the JSON path extraction is exactly the part that differs by backend.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.database.orm import OrmDatabase
from app.repositories.session_repository import SessionRepository


def child_payload(session_id: str, parent_id: str, clip_id: str, *, status: str = "completed") -> dict[str, Any]:
    return {
        "id": session_id,
        "name": session_id,
        "status": status,
        "parentSessionId": parent_id,
        "clipSource": {"clipId": clip_id, "label": "Student A", "fileName": "clip-1.mp4", "revision": 0},
        "outputs": {},
    }


def seed(tmp_path: Path, payloads: list[dict[str, Any]]) -> SessionRepository:
    async def _run() -> SessionRepository:
        repository = SessionRepository(OrmDatabase(tmp_path / "sessions.sqlite3"))
        await repository.database.initialize()
        for payload in payloads:
            await repository.write(payload)
        return repository

    return asyncio.run(_run())


def test_children_are_filtered_by_parent_and_clip(tmp_path: Path) -> None:
    repository = seed(
        tmp_path,
        [
            {"id": "parent-1", "name": "parent-1", "status": "cropped", "outputs": {}},
            child_payload("child-a", "parent-1", "clip-a"),
            child_payload("child-b", "parent-1", "clip-b"),
            child_payload("child-other", "parent-2", "clip-a"),
        ],
    )

    found = asyncio.run(repository.find_clip_children("parent-1", "clip-a"))

    assert [child["id"] for child in found] == ["child-a"]
    assert found[0]["status"] == "completed"
    assert found[0]["clipSource"]["fileName"] == "clip-1.mp4"


def test_duplicates_from_an_older_build_come_back_newest_first(tmp_path: Path) -> None:
    """Rows created before the invariant existed still have to be resolvable."""
    repository = seed(
        tmp_path,
        [
            {"id": "parent-1", "name": "parent-1", "status": "cropped", "outputs": {}},
            child_payload("child-old", "parent-1", "clip-a"),
        ],
    )

    async def _add_newer() -> list[dict[str, Any]]:
        await repository.write(child_payload("child-new", "parent-1", "clip-a", status="failed"))
        return await repository.find_clip_children("parent-1", "clip-a")

    found = asyncio.run(_add_newer())

    assert [child["id"] for child in found] == ["child-new", "child-old"]


def test_a_clip_with_no_child_returns_nothing(tmp_path: Path) -> None:
    repository = seed(
        tmp_path,
        [
            {"id": "parent-1", "name": "parent-1", "status": "cropped", "outputs": {}},
            child_payload("child-a", "parent-1", "clip-a"),
        ],
    )

    assert asyncio.run(repository.find_clip_children("parent-1", "clip-z")) == []
