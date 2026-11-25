"""A clip is only ever cut to, and assessed from, a file that is really its own.

Two bugs shared one cause: a clip with no file of its own was treated as if it
had one.

* Re-cropping a clip that auto-crop proposed but nobody exported resolved to
  ``<session>-clip-1-r1.mp4`` for *every* such clip (no ``planId``, no
  ``exportIndex``), so the second recrop adopted the first clip's finished MP4
  and its child assessment scored the wrong student.
* ``Path("")`` is ``Path(".")``, which exists, so a draft clip passed the
  "clip file exists" guard; a completed child was then repointed at ``.`` and
  its finished assessment destroyed by the re-run.
"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

import pytest

from app.core.exceptions import AppError
from app.domain.sessions import existing_file

from tests.test_clip_assess_idempotent import _build as _build_assess, _children
from tests.test_clip_batch_assess import _add_clips
from tests.test_clip_recrop_job import _build, _clip, _finish_job, _split_and_export


def _auto_crop_drafts(sessions: Any, count: int = 2) -> list[str]:
    """What auto-crop leaves behind: ranges with no file, no plan, no index."""
    clips = [
        {
            "id": f"draft-{index}",
            "label": f"Student {index + 1}",
            "start": 100.0 * index,
            "end": 100.0 * index + 90.0,
            "kind": "session",
            "fileName": None,
            "url": None,
            "absolutePath": None,
            "sizeBytes": 0,
            "isDraft": True,
        }
        for index in range(count)
    ]
    sessions.current["outputs"]["videoClips"] = clips
    sessions.current["status"] = "cropped"
    return [clip["id"] for clip in clips]


# --- H1 ---------------------------------------------------------------------


def test_recropping_a_never_exported_clip_only_moves_its_range(tmp_path: Path) -> None:
    service, sessions, media, jobs = _build(tmp_path)
    clip_id = _auto_crop_drafts(sessions)[0]

    result = asyncio.run(service.request_clip_recrop("sess-1", clip_id, 10.0, 80.0))

    clip = _clip(sessions)
    assert (clip["start"], clip["end"]) == (10.0, 80.0)
    assert clip["isDraft"] is True
    assert "revision" not in clip, "nothing was cut at revision 0, so there is no revision to bump"
    assert jobs.enqueued == [], "a clip with no file has nothing to re-cut"
    assert media.crops == []
    assert result["job"] is None
    assert result["clipExport"] == {}
    assert result["clip"]["start"] == 10.0


def test_two_drafts_recropped_in_sequence_never_share_a_file(tmp_path: Path) -> None:
    service, sessions, media, jobs = _build(tmp_path)
    first, second = _auto_crop_drafts(sessions)
    asyncio.run(service.request_clip_recrop("sess-1", first, 10.0, 80.0))
    asyncio.run(service.request_clip_recrop("sess-1", second, 110.0, 190.0))

    assert jobs.enqueued == []
    # The next "Export clips" re-plans from the timeline and cuts each clip
    # under its own index.
    asyncio.run(service.request_clip_export("sess-1", [100.0], [], None))
    asyncio.run(service.export_clips_by_id("sess-1"))

    clips = sessions.current["outputs"]["videoClips"]
    paths = [clip["absolutePath"] for clip in clips]
    assert len(set(paths)) == len(paths) == 2
    assert [path.name for path in media.crops] == ["clip-1.mp4", "clip-2.mp4"]


def test_recropping_an_exported_clip_still_queues_the_scoped_export(tmp_path: Path) -> None:
    service, sessions, media, jobs = _build(tmp_path)
    _split_and_export(service)
    _finish_job(jobs, "job-1")
    clip_id = str(_clip(sessions, 1)["id"])

    result = asyncio.run(service.request_clip_recrop("sess-1", clip_id, 210.0, 330.0))
    media.crops.clear()
    asyncio.run(service.export_clips_by_id("sess-1", {"clipIds": [clip_id]}))

    assert result["job"]["id"] == "job-2"
    assert _clip(sessions, 1)["revision"] == 1
    assert [path.parts[-2:] for path in media.crops] == [(sessions.current["clipExport"]["planId"], "clip-2-r1.mp4")]


def test_a_legacy_clip_with_an_index_and_no_plan_keeps_its_flat_name(tmp_path: Path) -> None:
    service, sessions, media, _jobs = _build(tmp_path)
    legacy = {"id": "legacy", "start": 0.0, "end": 90.0, "exportIndex": 2, "isDraft": True}

    asyncio.run(media.materialize_clip(session_id="sess-1", clip=legacy, video_path=tmp_path / "recording.mp4"))

    assert legacy["fileName"] == "sess-1-clip-3.mp4"
    assert media.crops[0].parent.name == "sess-1"


def test_materialize_refuses_to_guess_an_index(tmp_path: Path) -> None:
    _service, _sessions, media, _jobs = _build(tmp_path)
    anonymous = {"id": "x", "start": 0.0, "end": 90.0, "isDraft": True}

    with pytest.raises(AppError):
        asyncio.run(media.materialize_clip(session_id="sess-1", clip=anonymous, video_path=tmp_path / "recording.mp4"))
    with pytest.raises(AppError):
        asyncio.run(
            media.materialize_clip(
                session_id="sess-1", clip=dict(anonymous), video_path=tmp_path / "recording.mp4", plan_id="plan"
            )
        )
    assert media.crops == []


# --- H2 ---------------------------------------------------------------------


def test_existing_file_resolves_only_real_files(tmp_path: Path) -> None:
    real = tmp_path / "a.mp4"
    real.write_bytes(b"x")
    assert existing_file({"absolutePath": str(real)}) == real
    assert existing_file({"absolutePath": str(real), "isDraft": True}) is None
    assert existing_file({"absolutePath": ""}) is None
    assert existing_file({"absolutePath": None}) is None
    assert existing_file({}) is None
    assert existing_file(None) is None
    assert existing_file({"absolutePath": str(tmp_path)}) is None, "a directory is not a file"
    assert existing_file({"absolutePath": str(tmp_path / "gone.mp4")}) is None


def _make_draft(store: Any, index: int = 0) -> None:
    clip = store.sessions["parent-1"]["outputs"]["videoClips"][index]
    for key in ("fileName", "url", "absolutePath"):
        clip[key] = None
    clip["isDraft"] = True


def test_assessing_a_draft_clip_is_refused_and_creates_no_child(tmp_path: Path) -> None:
    service, store, jobs, _maintenance, _tmp = _build_assess(tmp_path)
    _make_draft(store)

    with pytest.raises(AppError) as raised:
        asyncio.run(service.assess_clip("parent-1", "clip-a"))

    assert raised.value.status_code == 409
    assert "not been cut" in raised.value.message
    assert _children(store) == [] and jobs.enqueued == []


def test_a_draft_that_still_names_a_stale_path_is_refused(tmp_path: Path) -> None:
    service, store, jobs, _maintenance, _tmp = _build_assess(tmp_path)
    store.sessions["parent-1"]["outputs"]["videoClips"][0]["isDraft"] = True  # path still on disk

    with pytest.raises(AppError):
        asyncio.run(service.assess_clip("parent-1", "clip-a"))
    assert jobs.enqueued == []


def test_rerunning_a_completed_child_whose_clip_became_a_draft_destroys_nothing(tmp_path: Path) -> None:
    service, store, _jobs, maintenance, _tmp = _build_assess(tmp_path)
    asyncio.run(service.assess_clip("parent-1", "clip-a"))
    child_id = _children(store)[0]["id"]
    store.sessions[child_id]["status"] = "completed"
    store.sessions[child_id]["outputs"] = {"scores": {"absolutePath": "scores.json"}}
    before = copy.deepcopy(store.sessions[child_id])
    _make_draft(store)

    with pytest.raises(AppError):
        asyncio.run(service.assess_clip("parent-1", "clip-a"))

    assert maintenance.reruns == [], "the finished assessment was re-run (and its scores deleted)"
    assert store.sessions[child_id] == before, "the child was repointed away from its video"


def test_an_empty_case_study_path_is_refused_rather_than_read_as_dot(tmp_path: Path) -> None:
    service, store, jobs, _maintenance, _tmp = _build_assess(tmp_path)
    store.sessions["parent-1"]["files"]["caseStudy"]["absolutePath"] = ""

    with pytest.raises(AppError) as raised:
        asyncio.run(service.assess_clip("parent-1", "clip-a"))

    assert "case study" in raised.value.message.lower()
    assert jobs.enqueued == []


def test_a_batch_refuses_the_draft_and_queues_the_cut_clip(tmp_path: Path) -> None:
    service, store, jobs, _maintenance, _tmp = _build_assess(tmp_path)
    clip_ids = _add_clips(store, tmp_path, 2)
    _make_draft(store, 0)

    result = asyncio.run(service.assess_clips("parent-1", clip_ids))

    by_id = {item["clipId"]: item for item in result["results"]}
    assert by_id[clip_ids[0]]["ok"] is False and by_id[clip_ids[0]]["statusCode"] == 409
    assert by_id[clip_ids[1]]["ok"] is True
    assert (result["queued"], result["failed"]) == (1, 1)
    assert len(jobs.enqueued) == 1
