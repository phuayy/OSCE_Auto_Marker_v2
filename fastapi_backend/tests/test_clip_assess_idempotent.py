"""One clip, one child session.

``POST /clips/{id}/assess`` used to mint a child session on every call, with
nothing but tab-local React state standing between a double click and two
children scoring the same student. The timeline then showed whichever the
session index happened to order first — which, ordered newest-first, was the
*older* run. These tests pin the invariant that replaced that: a repeat request
returns the child that is already running, and a request for a clip whose child
has finished re-runs that child in place, against the clip's *current* cut.
"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.services.clip_service import ClipService

from tests.fixtures.session_store import SessionUpdateMixin
from tests.test_clip_export_job import FakeEvents, FakeJobs, FakePipeline


class FakeRepository:
    """The one repository query the find-or-create path needs."""

    def __init__(self, store: "MultiSessionStore") -> None:
        self.store = store
        self.calls: list[tuple[str, str]] = []

    async def find_clip_children(self, parent_session_id: str, clip_id: str) -> list[dict[str, Any]]:
        self.calls.append((parent_session_id, clip_id))
        children = [
            session
            for session in self.store.sessions.values()
            if str(session.get("parentSessionId") or "") == parent_session_id
            and str((session.get("clipSource") or {}).get("clipId") or "") == str(clip_id)
        ]
        children.sort(key=lambda item: str(item.get("createdAt") or ""), reverse=True)
        return [
            {
                "id": child["id"],
                "status": child.get("status"),
                "createdAt": child.get("createdAt"),
                "clipSource": child.get("clipSource"),
            }
            for child in children
        ]


class MultiSessionStore(SessionUpdateMixin):
    """A session store holding a parent and whatever children get created."""

    def __init__(self, parent: dict[str, Any]) -> None:
        self.sessions: dict[str, dict[str, Any]] = {str(parent["id"]): copy.deepcopy(parent)}
        self.created: list[str] = []
        self.repository = FakeRepository(self)

    async def read(self, session_id: str) -> dict[str, Any]:
        try:
            return copy.deepcopy(self.sessions[str(session_id)])
        except KeyError as error:
            raise FileNotFoundError(f"Session not found: {session_id}") from error

    async def write(self, session: dict[str, Any]) -> None:
        self.sessions[str(session["id"])] = copy.deepcopy(session)

    async def create_named(self, session: dict[str, Any]) -> None:
        self.created.append(str(session["id"]))
        await self.write(session)

    def public_session(self, session: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(session)

    async def list_child_ids(self, parent_session_id: str) -> list[str]:
        return [
            str(session["id"])
            for session in self.sessions.values()
            if str(session.get("parentSessionId") or "") == parent_session_id
        ]


class FakeMaintenance:
    """Records what the clip path asks the maintenance service to re-run."""

    def __init__(self, store: MultiSessionStore) -> None:
        self.store = store
        self.reruns: list[str] = []
        self.video_at_rerun: list[str] = []

    async def rerun_session(self, session_id: str) -> dict[str, Any]:
        self.reruns.append(session_id)
        session = self.store.sessions[str(session_id)]
        self.video_at_rerun.append(str(((session.get("files") or {}).get("video") or {}).get("absolutePath") or ""))
        session["status"] = "queued"
        session["outputs"] = {}
        return {"session": copy.deepcopy(session), "job": {"id": f"rerun-{len(self.reruns)}"}}


class StubMedia:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings


def _build(tmp_path: Path, *, with_maintenance: bool = True):
    settings = Settings(root_dir=tmp_path, backend_root=tmp_path, auto_crop_min_clip_seconds=1.0)
    case_study = tmp_path / "case.pdf"
    case_study.write_bytes(b"%PDF-1.4")
    clip_file = tmp_path / "clip-1.mp4"
    clip_file.write_bytes(b"clip")
    parent = {
        "id": "parent-1",
        "name": "Long Station",
        "status": "cropped",
        "workflow": "long",
        "createdAt": "2026-01-01T00:00:00Z",
        "files": {
            "video": {"absolutePath": str(tmp_path / "recording.mp4"), "originalName": "recording.mp4"},
            "caseStudy": {"absolutePath": str(case_study), "originalName": "case.pdf", "fileName": "case.pdf"},
        },
        "outputs": {
            "videoClips": [
                {
                    "id": "clip-a",
                    "label": "Student A",
                    "kind": "session",
                    "start": 0.0,
                    "end": 120.0,
                    "exportIndex": 0,
                    "planId": "plan-1",
                    "revision": 0,
                    "fileName": "clip-1.mp4",
                    "absolutePath": str(clip_file),
                    "url": "/media/clips/parent-1/plan-1/clip-1.mp4",
                    "sizeBytes": 4,
                    "isDraft": False,
                }
            ]
        },
    }
    store = MultiSessionStore(parent)
    maintenance = FakeMaintenance(store) if with_maintenance else None
    jobs = FakeJobs()
    service = ClipService(
        store,
        FakeEvents(),
        StubMedia(settings),
        FakePipeline(),
        jobs=jobs,
        maintenance=maintenance,
    )
    return service, store, jobs, maintenance, tmp_path


def _children(store: MultiSessionStore) -> list[dict[str, Any]]:
    return [session for session in store.sessions.values() if session.get("parentSessionId")]


def test_the_first_request_creates_one_child_and_queues_it(tmp_path: Path) -> None:
    service, store, jobs, _maintenance, _tmp = _build(tmp_path)

    result = asyncio.run(service.assess_clip("parent-1", "clip-a"))

    assert result["reused"] is False
    assert len(_children(store)) == 1
    assert [task for _sid, task, _payload in jobs.enqueued] == ["process_session"]
    child = _children(store)[0]
    # The child records which cut it assessed, so a later recrop is detectable.
    assert child["clipSource"]["fileName"] == "clip-1.mp4"
    assert child["clipSource"]["revision"] == 0


def test_a_repeat_request_while_the_child_runs_returns_that_child(tmp_path: Path) -> None:
    service, store, jobs, maintenance, _tmp = _build(tmp_path)
    first = asyncio.run(service.assess_clip("parent-1", "clip-a"))

    second = asyncio.run(service.assess_clip("parent-1", "clip-a"))

    assert second["reused"] is True
    assert second["session"]["id"] == first["session"]["id"]
    assert len(_children(store)) == 1, "a second child session was created for one clip"
    assert len(jobs.enqueued) == 1, "the clip was queued twice"
    assert maintenance.reruns == [], "an in-flight child was needlessly re-run"


def test_concurrent_requests_for_one_clip_create_exactly_one_child(tmp_path: Path) -> None:
    """The double-click / second-tab / batch-run race, run for real."""
    service, store, jobs, _maintenance, _tmp = _build(tmp_path)

    async def _race() -> list[dict[str, Any]]:
        return list(await asyncio.gather(*(service.assess_clip("parent-1", "clip-a") for _ in range(5))))

    results = asyncio.run(_race())

    assert len(_children(store)) == 1
    assert len(jobs.enqueued) == 1
    assert len({result["session"]["id"] for result in results}) == 1
    assert sum(1 for result in results if result["reused"]) == 4


def test_a_finished_child_is_re_run_against_the_clips_current_cut(tmp_path: Path) -> None:
    service, store, jobs, maintenance, tmp = _build(tmp_path)
    asyncio.run(service.assess_clip("parent-1", "clip-a"))
    child_id = _children(store)[0]["id"]
    store.sessions[child_id]["status"] = "completed"
    # The clip has since been re-cut: new revision, new file.
    recut = tmp / "clip-1-r1.mp4"
    recut.write_bytes(b"new-clip")
    clip = store.sessions["parent-1"]["outputs"]["videoClips"][0]
    clip.update(
        {
            "revision": 1,
            "fileName": "clip-1-r1.mp4",
            "absolutePath": str(recut),
            "url": "/media/clips/parent-1/plan-1/clip-1-r1.mp4",
        }
    )

    result = asyncio.run(service.assess_clip("parent-1", "clip-a"))

    assert result["reused"] is True
    assert len(_children(store)) == 1, "a finished child was duplicated instead of re-run"
    assert maintenance.reruns == [child_id]
    # Refreshed *before* the re-run: the job may be claimed the moment it is
    # enqueued, and it must read the cut this request is about.
    assert maintenance.video_at_rerun == [str(recut)]
    child = store.sessions[child_id]
    assert child["clipSource"]["fileName"] == "clip-1-r1.mp4"
    assert child["clipSource"]["revision"] == 1
    assert len(jobs.enqueued) == 1, "the re-run went through the queue twice"


def test_a_failed_child_is_re_run_rather_than_duplicated(tmp_path: Path) -> None:
    service, store, _jobs, maintenance, _tmp = _build(tmp_path)
    asyncio.run(service.assess_clip("parent-1", "clip-a"))
    child_id = _children(store)[0]["id"]
    store.sessions[child_id]["status"] = "failed"

    result = asyncio.run(service.assess_clip("parent-1", "clip-a"))

    assert result["reused"] is True
    assert maintenance.reruns == [child_id]
    assert len(_children(store)) == 1


def test_without_a_maintenance_service_a_finished_child_reports_the_missing_dependency(
    tmp_path: Path,
) -> None:
    service, store, _jobs, _maintenance, _tmp = _build(tmp_path, with_maintenance=False)
    asyncio.run(service.assess_clip("parent-1", "clip-a"))
    store.sessions[_children(store)[0]["id"]]["status"] = "completed"

    with pytest.raises(RuntimeError, match="maintenance"):
        asyncio.run(service.assess_clip("parent-1", "clip-a"))
