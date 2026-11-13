""""Run selected" queues every selected clip as one admission.

The batch used to be one ``/clips/{id}/assess`` per clip, each charged against
the account's hourly expensive-operation budget (default 10), so a 15-student
recording had 10 clips queued and 5 refused with 429.
"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path

from app.core.rate_limit import FixedWindowRateLimiter
from app.schemas.sessions import AssessClipsRequest

from tests.test_clip_assess_idempotent import _build, _children
from tests.test_routes import build_test_client


def _add_clips(store, tmp_path: Path, count: int) -> list[str]:
    parent = store.sessions["parent-1"]
    template = parent["outputs"]["videoClips"][0]
    ids = [template["id"]]
    for index in range(1, count):
        clip_file = tmp_path / f"clip-{index + 1}.mp4"
        clip_file.write_bytes(b"clip")
        clip = copy.deepcopy(template)
        clip.update({"id": f"clip-{index}", "label": f"Student {index}", "exportIndex": index,
                     "fileName": clip_file.name, "absolutePath": str(clip_file)})
        parent["outputs"]["videoClips"].append(clip)
        ids.append(clip["id"])
    return ids


def test_a_batch_of_fifteen_clips_queues_all_fifteen(tmp_path: Path) -> None:
    service, store, jobs, _maintenance, _tmp = _build(tmp_path)
    clip_ids = _add_clips(store, tmp_path, 15)

    result = asyncio.run(service.assess_clips("parent-1", clip_ids))

    assert result["queued"] == 15 and result["failed"] == 0
    assert [item["clipId"] for item in result["results"]] == clip_ids
    assert len(_children(store)) == 15
    assert len(jobs.enqueued) == 15


def test_one_bad_clip_does_not_stop_the_rest(tmp_path: Path) -> None:
    service, store, jobs, _maintenance, _tmp = _build(tmp_path)
    clip_ids = _add_clips(store, tmp_path, 3)
    Path(store.sessions["parent-1"]["outputs"]["videoClips"][1]["absolutePath"]).unlink()

    result = asyncio.run(service.assess_clips("parent-1", [*clip_ids, "no-such-clip"]))

    by_id = {item["clipId"]: item for item in result["results"]}
    assert result["queued"] == 2 and result["failed"] == 2
    assert by_id["clip-1"]["ok"] is False and "missing" in by_id["clip-1"]["error"].lower()
    assert by_id["no-such-clip"]["statusCode"] == 404
    assert len(jobs.enqueued) == 2


def test_request_dedupes_and_drops_blank_ids() -> None:
    request = AssessClipsRequest.model_validate({"clipIds": ["a", " a ", "", "b"]})
    assert request.clip_ids == ["a", "b"]


def test_the_batch_route_is_charged_once_against_the_hourly_budget(tmp_path: Path, monkeypatch) -> None:
    client = build_test_client(tmp_path)
    container = client.app.state.container
    container.expensive_operation_rate_limiter = FixedWindowRateLimiter(max_attempts=1, window_seconds=3600)
    token = client.post('/api/auth/login', json={'username': 'admin', 'password': 'admin'}).json()['token']
    headers = {'Authorization': f'Bearer {token}'}
    asyncio.run(container.sessions.write({'id': 's1', 'name': 'Session'}))
    calls: list[list[str]] = []

    async def assess_clips(session_id, clip_ids, *, actor=None):
        calls.append(list(clip_ids))
        return {'results': [{'clipId': c, 'ok': True} for c in clip_ids], 'queued': len(clip_ids), 'failed': 0}

    monkeypatch.setattr(container.clips, 'assess_clips', assess_clips)
    clip_ids = [f'clip-{n}' for n in range(15)]
    response = client.post('/api/sessions/s1/clips/assess', headers=headers, json={'clipIds': clip_ids})

    assert response.status_code == 202
    assert response.json()['queued'] == 15
    assert calls == [clip_ids]
    # The one unit of budget is now spent: a second action is refused.
    assert client.post('/api/sessions/s1/clips/assess', headers=headers, json={'clipIds': ['x']}).status_code == 429
