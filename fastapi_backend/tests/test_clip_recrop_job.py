"""Re-cropping one clip, as a durable job.

A recrop used to run ffmpeg inside ``POST /clips/{id}/recrop``: one crop — a
stream copy that falls back to a re-encode — held the HTTP connection, died
with the process, reported no progress and left the file it replaced on disk.
It is now an *export of a single clip*: the same ``export_clips`` job, the same
``session.clipExport`` progress record, the same adoption-on-retry rule. These
tests pin the three properties that makes true, and the one field the design
rests on — ``revision``, which stops the job adopting the very cut it is
replacing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.core.exceptions import AppError
from app.domain.enums import ClipExportScope
from app.services.clip_service import ClipService

from tests.test_clip_export_job import FakeEvents, FakeJobs, FakePipeline, FakeSessions, RecordingMedia
from app.core.config import Settings


class ChildAwareSessions(FakeSessions):
    """Session store that can also answer "what clip children exist?".

    The recrop deletes the cut it replaced — unless a child assessment still
    plays that file, which is the one case where the old MP4 has to stay.
    """

    def __init__(self, initial_session: dict[str, Any]) -> None:
        super().__init__(initial_session)
        self.children: dict[str, dict[str, Any]] = {}

    async def list_child_ids(self, _parent_session_id: str) -> list[str]:
        return list(self.children)

    async def read(self, session_id: str) -> dict[str, Any]:
        if session_id in self.children:
            return self.children[session_id]
        return await super().read(session_id)


def _build(tmp_path: Path, *, fail_on_index: int | None = None):
    settings = Settings(root_dir=tmp_path, backend_root=tmp_path, auto_crop_min_clip_seconds=1.0)
    video_path = tmp_path / "recording.mp4"
    video_path.write_bytes(b"video")
    session = {
        "id": "sess-1",
        "name": "Long Station",
        "status": "uploaded",
        "workflow": "long",
        "files": {"video": {"absolutePath": str(video_path)}},
        "outputs": {},
    }
    sessions = ChildAwareSessions(session)
    media = RecordingMedia(settings, fail_on_index=fail_on_index)
    jobs = FakeJobs()
    service = ClipService(sessions, FakeEvents(), media, FakePipeline(), jobs=jobs)
    return service, sessions, media, jobs


def _split_and_export(service: ClipService) -> None:
    """The state every recrop starts from: a plan, exported."""
    asyncio.run(service.request_clip_export("sess-1", [200.0, 400.0], [], None))
    asyncio.run(service.export_clips_by_id("sess-1"))


def _clip(sessions: FakeSessions, index: int = 0) -> dict[str, Any]:
    return sessions.current["outputs"]["videoClips"][index]


def _finish_job(jobs: FakeJobs, job_id: str) -> None:
    """Mark a queued job terminal, as the queue would once it had run."""
    jobs.repository.rows[job_id]["status"] = "succeeded"


def test_the_request_replans_and_queues_without_cutting(tmp_path: Path) -> None:
    service, sessions, media, jobs = _build(tmp_path)
    _split_and_export(service)
    _finish_job(jobs, "job-1")
    crops_after_split = len(media.crops)
    clip_id = str(_clip(sessions)["id"])

    result = asyncio.run(service.request_clip_recrop("sess-1", clip_id, 210.0, 330.0))

    assert len(media.crops) == crops_after_split, "the request cut the clip inline"
    clip = _clip(sessions)
    assert (clip["start"], clip["end"]) == (210.0, 330.0)
    assert clip["revision"] == 1
    # A draft is a range with no file: that is what keeps the re-cut clip out
    # of assessment until the new MP4 lands.
    assert clip["isDraft"] is True
    assert "url" not in clip and "absolutePath" not in clip
    assert clip["supersededFile"].endswith("clip-1.mp4")

    export = sessions.current["clipExport"]
    assert export["status"] == "queued"
    assert export["scope"] == ClipExportScope.CLIP
    assert export["clipIds"] == [clip_id]
    assert (export["total"], export["completed"]) == (1, 0)
    assert export["jobId"] == "job-2"
    session_id, task_type, payload = jobs.enqueued[-1]
    assert (session_id, task_type) == ("sess-1", "export_clips")
    assert payload["clipIds"] == [clip_id] and payload["scope"] == str(ClipExportScope.CLIP)
    assert result["job"]["id"] == "job-2"


def test_the_job_cuts_only_the_requested_clip_under_a_new_revision(tmp_path: Path) -> None:
    service, sessions, media, jobs = _build(tmp_path)
    _split_and_export(service)
    _finish_job(jobs, "job-1")
    clip_id = str(_clip(sessions)["id"])
    asyncio.run(service.request_clip_recrop("sess-1", clip_id, 210.0, 330.0))
    media.crops.clear()

    result = asyncio.run(service.export_clips_by_id("sess-1", {"clipIds": [clip_id]}))

    assert len(media.crops) == 1, "a recrop re-cut clips it was not asked for"
    # The revision suffix is what stops the job adopting the cut it replaces.
    assert media.crops[0].name == "clip-1-r1.mp4"
    assert result["clipCount"] == 1
    clip = _clip(sessions)
    assert clip["isDraft"] is False
    assert clip["fileName"] == "clip-1-r1.mp4"
    assert "supersededFile" not in clip, "the replaced cut is forgotten once the new one is durable"
    export = sessions.current["clipExport"]
    assert (export["status"], export["completed"], export["total"]) == ("completed", 1, 1)
    # Every other clip in the plan is untouched.
    assert [item["fileName"] for item in sessions.current["outputs"]["videoClips"][1:]] == [
        "clip-2.mp4",
        "clip-3.mp4",
    ]


def test_the_superseded_cut_is_deleted_once_the_new_one_is_durable(tmp_path: Path) -> None:
    service, sessions, media, jobs = _build(tmp_path)
    _split_and_export(service)
    _finish_job(jobs, "job-1")
    clip_id = str(_clip(sessions)["id"])
    old_file = Path(_clip(sessions)["absolutePath"])
    assert old_file.exists()

    asyncio.run(service.request_clip_recrop("sess-1", clip_id, 210.0, 330.0))
    asyncio.run(service.export_clips_by_id("sess-1", {"clipIds": [clip_id]}))

    assert not old_file.exists(), "the replaced cut was left behind"
    assert Path(_clip(sessions)["absolutePath"]).exists()
    _ = media


def test_a_superseded_cut_a_child_still_plays_is_kept(tmp_path: Path) -> None:
    """A child assessed from the old cut keeps playing — and re-running — it."""
    service, sessions, _media, jobs = _build(tmp_path)
    _split_and_export(service)
    _finish_job(jobs, "job-1")
    clip = _clip(sessions)
    clip_id = str(clip["id"])
    old_file = Path(clip["absolutePath"])
    sessions.children["child-1"] = {
        "id": "child-1",
        "parentSessionId": "sess-1",
        "files": {"video": {"absolutePath": str(old_file)}},
    }

    asyncio.run(service.request_clip_recrop("sess-1", clip_id, 210.0, 330.0))
    asyncio.run(service.export_clips_by_id("sess-1", {"clipIds": [clip_id]}))

    assert old_file.exists(), "deleting this file would break the child's playback and re-run"
    assert _clip(sessions)["fileName"] == "clip-1-r1.mp4"


def test_a_retried_recrop_adopts_the_finished_cut(tmp_path: Path) -> None:
    service, sessions, media, jobs = _build(tmp_path)
    _split_and_export(service)
    _finish_job(jobs, "job-1")
    clip_id = str(_clip(sessions)["id"])
    asyncio.run(service.request_clip_recrop("sess-1", clip_id, 210.0, 330.0))
    asyncio.run(service.export_clips_by_id("sess-1", {"clipIds": [clip_id]}))
    media.crops.clear()

    asyncio.run(service.export_clips_by_id("sess-1", {"clipIds": [clip_id]}))

    assert media.crops == [], "the retry re-cut a clip that was already finished"
    assert sessions.current["clipExport"]["status"] == "completed"


def test_a_recrop_is_refused_while_an_export_is_in_flight(tmp_path: Path) -> None:
    service, sessions, _media, _jobs = _build(tmp_path)
    asyncio.run(service.request_clip_export("sess-1", [200.0, 400.0], [], None))
    clip_id = str(_clip(sessions)["id"])

    with pytest.raises(AppError) as error:
        asyncio.run(service.request_clip_recrop("sess-1", clip_id, 210.0, 330.0))

    assert error.value.status_code == 409


def test_an_intermission_cannot_be_recropped(tmp_path: Path) -> None:
    service, sessions, _media, jobs = _build(tmp_path)
    asyncio.run(service.request_clip_export("sess-1", [200.0, 400.0], [], ["session", "intermission", "session"]))
    asyncio.run(service.export_clips_by_id("sess-1"))
    _finish_job(jobs, "job-1")
    intermission = next(
        clip for clip in sessions.current["outputs"]["videoClips"] if clip.get("kind") == "intermission"
    )

    with pytest.raises(AppError) as error:
        asyncio.run(service.request_clip_recrop("sess-1", str(intermission["id"]), 210.0, 330.0))

    assert error.value.status_code == 400


def test_a_recrop_whose_clip_vanished_fails_without_retrying(tmp_path: Path) -> None:
    """The plan's clips were replaced by a re-split before the job ran."""
    service, sessions, _media, jobs = _build(tmp_path)
    _split_and_export(service)
    _finish_job(jobs, "job-1")

    with pytest.raises(AppError) as error:
        asyncio.run(service.export_clips_by_id("sess-1", {"clipIds": ["clip-that-is-gone"]}))

    assert error.value.status_code == 400
    assert error.value.retryable is False


def test_a_plan_export_is_unchanged_by_the_scope_field(tmp_path: Path) -> None:
    """Regression guard: the whole-split path keeps its historical behaviour."""
    service, sessions, media, _jobs = _build(tmp_path)
    asyncio.run(service.request_clip_export("sess-1", [200.0, 400.0], [], None))

    asyncio.run(service.export_clips_by_id("sess-1"))

    assert [path.name for path in media.crops] == ["clip-1.mp4", "clip-2.mp4", "clip-3.mp4"]
    export = sessions.current["clipExport"]
    assert export["scope"] == ClipExportScope.PLAN
    assert export["clipIds"] is None
    assert (export["status"], export["completed"], export["total"]) == ("completed", 3, 3)
