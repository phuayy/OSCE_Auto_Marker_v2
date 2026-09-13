"""Wiring tests for the auto-crop segmentation method (bells vs person/RT-DETR).

Covers the full choice path: upload schema -> session persistence ->
ClipService detector selection -> graceful fallback to bell detection when the
person detector fails.
"""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.services.clip_service import ClipService

from .test_routes import build_test_client
from tests.fixtures.session_store import SessionUpdateMixin
from tests.fixtures.events import RecordingEvents as FakeEvents


class FakeSessions(SessionUpdateMixin):
    def __init__(self, initial_session: dict[str, Any]) -> None:
        self.current = copy.deepcopy(initial_session)

    async def read(self, session_id: str) -> dict[str, Any]:
        if str(self.current.get("id")) != str(session_id):
            raise FileNotFoundError(f"Session not found: {session_id}")
        return copy.deepcopy(self.current)

    async def write(self, session: dict[str, Any]) -> None:
        self.current = copy.deepcopy(session)

    def public_session(self, session: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(session)


class FakeMedia:
    """Records which detector ran; configurable person-detector failure.

    ``person_progress`` is the sequence of percentages the fake detector
    reports before returning — how the tests drive the progress plumbing
    without a subprocess.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        person_error: Exception | None = None,
        person_progress: tuple[float, ...] = (),
    ) -> None:
        self.settings = settings
        self.person_error = person_error
        self.person_progress = person_progress
        self.person_calls: list[dict[str, Any]] = []
        self.bell_calls: list[dict[str, Any]] = []

    async def get_video_duration_seconds(self, _path: Path) -> float:
        return 120.0

    async def detect_person_clip_ranges_with_python(
        self,
        source_path: Path,
        video_duration_seconds: float,
        *,
        session_id: str | None = None,
        on_progress: Any | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.person_calls.append({"path": source_path, "sessionId": session_id, "options": options})
        for percent in self.person_progress:
            if on_progress is not None:
                await on_progress(percent)
        if self.person_error is not None:
            raise self.person_error
        return {
            "clipRanges": [{"start": 0.0, "end": 60.0}, {"start": 70.0, "end": 120.0}],
            # Full timeline partition: sessions + the greyed intermission gap.
            "timelineSegments": [
                {"start": 0.0, "end": 60.0, "kind": "session", "personCount": 2, "studentIndex": 1},
                {"start": 60.0, "end": 70.0, "kind": "intermission", "personCount": 1},
                {"start": 70.0, "end": 120.0, "kind": "session", "personCount": 2, "studentIndex": 2},
            ],
            "source": {"type": "person_detection_rtdetr", "usedTrigger": "person_presence"},
        }

    async def detect_bell_clip_ranges_with_python(
        self,
        source_path: Path,
        video_duration_seconds: float,
        *,
        sample_rate: int,
    ) -> dict[str, Any]:
        self.bell_calls.append({"path": source_path, "sampleRate": sample_rate})
        return {
            "clipRanges": [{"start": 0.0, "end": 120.0}],
            "source": {"type": "bell_detection_python_librosa", "usedTrigger": "hybrid"},
        }

    def build_clip_drafts_from_ranges(
        self,
        clip_ranges: list[dict[str, Any]],
        _video_duration_seconds: float,
        source_meta: dict[str, Any],
        label_overrides: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        _ = label_overrides
        return [
            {
                "id": f"clip-{index}",
                "start": item["start"],
                "end": item["end"],
                "kind": str(item.get("kind") or "session"),
                "source": source_meta,
            }
            for index, item in enumerate(clip_ranges, start=1)
        ]


def build_settings(tmp_path: Path) -> Settings:
    return Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        app_database_url="",
        database_url="",
    )


def build_long_session(
    tmp_path: Path,
    segmentation: str | None,
    segmentation_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    video_path = tmp_path / "long.mp4"
    video_path.write_bytes(b"video")
    return {
        "id": "session-long-1",
        "name": "Long Session",
        "status": "uploaded",
        "workflow": "long",
        "segmentation": segmentation,
        "segmentationOptions": segmentation_options,
        "files": {"video": {"absolutePath": str(video_path)}},
        "outputs": {},
        "error": None,
    }


def build_clip_service(
    tmp_path: Path,
    segmentation: str | None,
    *,
    person_error: Exception | None = None,
    person_progress: tuple[float, ...] = (),
    segmentation_options: dict[str, Any] | None = None,
):
    settings = build_settings(tmp_path)
    media = FakeMedia(settings, person_error=person_error, person_progress=person_progress)
    sessions = FakeSessions(build_long_session(tmp_path, segmentation, segmentation_options))
    events = FakeEvents()
    service = ClipService(sessions=sessions, events=events, media=media, pipeline=None, jobs=None)
    return service, media, sessions, events


class RecordingSessions(FakeSessions):
    """FakeSessions that keeps every write, not just the last one.

    Auto-crop's progress reports exist as much to keep the change stream alive
    as to move a bar, so "how many times was the session written, and what did
    each write say" is exactly what needs asserting.
    """

    def __init__(self, initial_session: dict[str, Any]) -> None:
        super().__init__(initial_session)
        self.writes: list[dict[str, Any]] = []

    async def write(self, session: dict[str, Any]) -> None:
        await super().write(session)
        self.writes.append(copy.deepcopy(session))

    def progress_readings(self) -> list[Any]:
        return [(write.get("pipeline") or {}).get("stepProgress") for write in self.writes]


def test_auto_crop_uses_person_detector_when_selected(tmp_path) -> None:
    service, media, sessions, _events = build_clip_service(tmp_path, "person")
    result = asyncio.run(service.auto_crop_session_by_id("session-long-1"))

    assert len(media.person_calls) == 1
    assert media.person_calls[0]["sessionId"] == "session-long-1"
    assert media.bell_calls == []
    assert result["source"]["type"] == "person_detection_rtdetr"
    # clipCount counts session clips only; the timeline partition also carries
    # the greyed intermission between them.
    assert result["clipCount"] == 2
    assert result["intermissionCount"] == 1
    assert sessions.current["status"] == "cropped"
    clips = sessions.current["outputs"]["videoClips"]
    assert [clip["kind"] for clip in clips] == ["session", "intermission", "session"]


def test_auto_crop_forwards_the_chosen_occupancy_preset(tmp_path) -> None:
    """The preset is chosen at upload but applied inside a job that may run
    minutes later in another process, so the resolved numbers have to travel on
    the session — not be re-derived from a table that could have moved."""
    service, media, _sessions, events = build_clip_service(
        tmp_path,
        "person",
        segmentation_options={
            "preset": "solo",
            "minPeople": 1,
            "minBoxHeightRatio": 0.4,
            "minSessionSeconds": 120.0,
        },
    )
    asyncio.run(service.auto_crop_session_by_id("session-long-1"))

    assert media.person_calls[0]["options"] == {
        "preset": "solo",
        "minPeople": 1,
        "minBoxHeightRatio": 0.4,
        "minSessionSeconds": 120.0,
    }
    # The operator can see which rule ran without opening the payload.
    log_messages = [payload.get("message", "") for _sid, kind, payload in events.items if kind == "log"]
    assert any("'solo'" in message for message in log_messages)


def test_auto_crop_falls_back_to_the_default_preset(tmp_path) -> None:
    """Sessions uploaded before presets existed carry no options at all, and a
    preset this build no longer ships must not kill a queued job either."""
    service, media, _sessions, _events = build_clip_service(tmp_path, "person")
    asyncio.run(service.auto_crop_session_by_id("session-long-1"))
    assert media.person_calls[0]["options"]["preset"] == "pair"

    service, media, _sessions, _events = build_clip_service(
        tmp_path, "person", segmentation_options={"preset": "retired-preset"}
    )
    asyncio.run(service.auto_crop_session_by_id("session-long-1"))
    assert media.person_calls[0]["options"]["preset"] == "pair"


def test_auto_crop_defaults_to_bell_detector(tmp_path) -> None:
    service, media, sessions, _events = build_clip_service(tmp_path, None)
    result = asyncio.run(service.auto_crop_session_by_id("session-long-1"))

    assert media.person_calls == []
    assert len(media.bell_calls) == 1
    assert result["source"]["type"] == "bell_detection_python_librosa"
    assert sessions.current["status"] == "cropped"


def test_auto_crop_ignores_unknown_segmentation_value(tmp_path) -> None:
    service, media, _sessions, _events = build_clip_service(tmp_path, "laser-eyes")
    asyncio.run(service.auto_crop_session_by_id("session-long-1"))

    assert media.person_calls == []
    assert len(media.bell_calls) == 1


def test_auto_crop_person_failure_falls_back_to_bells(tmp_path) -> None:
    service, media, sessions, events = build_clip_service(
        tmp_path,
        "person",
        person_error=RuntimeError("model weights unavailable"),
    )
    result = asyncio.run(service.auto_crop_session_by_id("session-long-1"))

    assert len(media.person_calls) == 1
    assert len(media.bell_calls) == 1
    assert result["source"]["type"] == "bell_detection_python_librosa"
    assert "person_detection_failed" in str(result["source"].get("fallbackFrom") or "")
    assert sessions.current["status"] == "cropped"
    # The fallback is surfaced in the live log, not silent.
    log_messages = [payload.get("message", "") for _sid, kind, payload in events.items if kind == "log"]
    assert any("Falling back to bell detection" in message for message in log_messages)


def build_recording_clip_service(
    tmp_path: Path,
    segmentation: str,
    *,
    person_error: Exception | None = None,
    person_progress: tuple[float, ...] = (),
):
    settings = build_settings(tmp_path)
    media = FakeMedia(settings, person_error=person_error, person_progress=person_progress)
    sessions = RecordingSessions(build_long_session(tmp_path, segmentation))
    service = ClipService(
        sessions=sessions, events=FakeEvents(), media=media, pipeline=None, jobs=None
    )
    return service, media, sessions


def test_auto_crop_names_the_running_detector_step(tmp_path) -> None:
    """The card's gauge only trusts a reading that belongs to the step the
    session is currently naming, so the step has to be named before it runs."""
    service, _media, sessions = build_recording_clip_service(tmp_path, "person")
    asyncio.run(service.auto_crop_session_by_id("session-long-1"))

    first_write = sessions.writes[0]
    assert first_write["status"] == "processing"
    assert first_write["pipeline"]["currentStep"] == "person_detection"
    assert first_write["pipeline"]["stepProgress"] is None


def test_auto_crop_defaults_name_the_bell_step(tmp_path) -> None:
    service, _media, sessions = build_recording_clip_service(tmp_path, "bells")
    asyncio.run(service.auto_crop_session_by_id("session-long-1"))

    assert sessions.writes[0]["pipeline"]["currentStep"] == "bell_detection"


def test_detector_progress_is_persisted_for_the_session_card(tmp_path) -> None:
    service, _media, sessions = build_recording_clip_service(
        tmp_path, "person", person_progress=(10.0, 55.0, 95.0)
    )
    asyncio.run(service.auto_crop_session_by_id("session-long-1"))

    readings = sessions.progress_readings()
    # Every reading reached the durable payload, in order. Each of those writes
    # is also a change-stream event, which is what stops a long detection run
    # from leaving the sessions table untouched for minutes.
    assert [value for value in readings if value is not None] == [10.0, 55.0, 95.0]


def test_finished_auto_crop_leaves_no_stale_progress(tmp_path) -> None:
    service, _media, sessions = build_recording_clip_service(
        tmp_path, "person", person_progress=(40.0,)
    )
    asyncio.run(service.auto_crop_session_by_id("session-long-1"))

    assert sessions.current["status"] == "cropped"
    assert sessions.current["pipeline"]["stepProgress"] is None
    assert sessions.current["pipeline"]["currentStep"] is None


def test_failed_auto_crop_leaves_no_stale_progress(tmp_path) -> None:
    # Person detection failing degrades to bells; make that fail too so the
    # whole job ends terminally and the failure path is the one exercised.
    service, media, sessions = build_recording_clip_service(
        tmp_path, "person", person_error=RuntimeError("weights gone"), person_progress=(30.0,)
    )

    async def failing_bells(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("no audio track")

    media.detect_bell_clip_ranges_with_python = failing_bells

    try:
        asyncio.run(service.auto_crop_session_by_id("session-long-1"))
    except RuntimeError:
        pass

    assert sessions.current["status"] == "failed"
    assert sessions.current["pipeline"]["stepProgress"] is None
    assert sessions.current["pipeline"]["currentStep"] is None


def test_fallback_to_bells_stops_gauging_the_dead_detector(tmp_path) -> None:
    """A frozen bar is worse than none: when person detection dies the card
    must stop reporting its last percentage for the whole bell pass."""
    service, _media, sessions = build_recording_clip_service(
        tmp_path,
        "person",
        person_error=RuntimeError("weights gone"),
        person_progress=(70.0,),
    )
    asyncio.run(service.auto_crop_session_by_id("session-long-1"))

    # Between the failure and the finish, the session was rewritten naming the
    # bell step with no reading carried over.
    handover = [
        write
        for write in sessions.writes
        if (write.get("pipeline") or {}).get("currentStep") == "bell_detection"
    ]
    assert handover, "the fallback must re-name the running step"
    assert handover[0]["pipeline"]["stepProgress"] is None
    assert sessions.current["status"] == "cropped"


def test_progress_for_a_step_that_is_no_longer_running_is_dropped(tmp_path) -> None:
    """A late reading from a subprocess reader thread must not resurrect a
    step that has already ended."""
    service, _media, sessions = build_recording_clip_service(tmp_path, "person")
    asyncio.run(service.auto_crop_session_by_id("session-long-1"))
    writes_before = len(sessions.writes)

    session = sessions.current
    asyncio.run(
        service._record_segmentation_progress(  # noqa: SLF001 — the rule is the unit under test
            session, "person_detection", 88.0, lock=asyncio.Lock()
        )
    )

    assert len(sessions.writes) == writes_before
    assert sessions.current["pipeline"]["stepProgress"] is None


def test_public_session_projects_clip_kinds() -> None:
    """public_session must not strip kind/personCount from clips — the manual
    crop editor seeds its greyed intermissions from them (dropping them made
    every segment render as Student N)."""
    from app.services.session_service import SessionService

    session = {
        "id": "session-long-1",
        "files": {"video": {}},
        "outputs": {
            "videoClips": [
                {"id": "c1", "label": "Student 1", "start": 0.0, "end": 60.0, "kind": "session", "personCount": 2},
                {"id": "c2", "label": "Intermission", "start": 60.0, "end": 70.0, "kind": "intermission", "personCount": 1},
                {"id": "c3", "label": "Legacy", "start": 70.0, "end": 120.0},  # pre-kinds clip
            ]
        },
    }
    clips = SessionService.public_session(session)["outputs"]["videoClips"]
    assert [(clip["kind"], clip["personCount"]) for clip in clips] == [
        ("session", 2),
        ("intermission", 1),
        ("session", None),  # legacy clips default to session
    ]


def test_manual_clip_ranges_keep_segment_index_after_sliver_drop(tmp_path) -> None:
    """A sub-minimum segment dropped by the boundary split must not shift the
    positional kind/label mapping of every segment after it — segmentIndex
    records the pre-filter position."""
    from app.pipeline.media import MediaPipeline

    media = MediaPipeline(build_settings(tmp_path), None, None, None)
    # Segments: [0-30], [30-30.3] (sliver, dropped), [30.3-60], [60-120].
    ranges = media.build_manual_clip_ranges(120.0, [30.0, 30.3, 60.0])
    assert [(r["start"], r["end"], r["segmentIndex"]) for r in ranges] == [
        (0.0, 30.0, 0),
        (30.3, 60.0, 2),
        (60.0, 120.0, 3),
    ]


def test_assess_clip_rejects_intermission_segments(tmp_path) -> None:
    import pytest

    from app.core.exceptions import AppError

    service, _media, sessions, _events = build_clip_service(tmp_path, "person")
    session = sessions.current
    session["outputs"] = {
        "videoClips": [
            {"id": "clip-gap", "kind": "intermission", "label": "Intermission", "start": 60.0, "end": 70.0},
        ]
    }

    with pytest.raises(AppError) as exc_info:
        asyncio.run(service.assess_clip("session-long-1", "clip-gap"))
    assert exc_info.value.status_code == 400
    assert "intermission" in str(exc_info.value).lower()


def test_initiate_persists_segmentation_choice(tmp_path) -> None:
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    response = client.post(
        "/api/uploads/initiate",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "workflow": "long",
            "autoProcess": True,
            "segmentation": "human",  # synonym normalises to "person"
            "files": [
                {"kind": "video", "originalName": "station.mp4", "mimeType": "video/mp4", "sizeBytes": 5},
                {"kind": "caseStudy", "originalName": "case.pdf", "mimeType": "application/pdf", "sizeBytes": 4},
            ],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["session"]["segmentation"] == "person"

    stored = asyncio.run(client.app.state.container.sessions.read(body["session"]["id"]))
    assert stored["segmentation"] == "person"


def test_detector_argv_carries_the_resolved_occupancy_rule(tmp_path) -> None:
    """The subprocess is handed explicit numbers, not a preset name to look up:
    the caller and the detector must not be able to resolve the same session
    differently."""
    import json
    from types import SimpleNamespace

    from app.core.process import CommandResult
    from app.pipeline.media import MediaPipeline

    recorded: dict[str, Any] = {}

    class ArgvRunner:
        async def run(self, command: str, args: list[str], label: str, **kwargs: Any) -> CommandResult:
            recorded["args"] = list(args)
            return CommandResult(
                stdout=json.dumps(
                    {
                        "clip_ranges": [{"start": 0.0, "end": 60.0}],
                        "timeline_segments": [],
                        "preset": "solo",
                        "min_people": 1,
                        "min_box_height_ratio": 0.4,
                        "min_session_seconds": 120.0,
                        "debug": {"workers": 1, "rejected_small_boxes": 42},
                    }
                ),
                stderr="",
            )

    settings = build_settings(tmp_path)
    script = settings.human_detector_script_path
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# stand-in", encoding="utf-8")
    media = MediaPipeline(
        settings,
        runner=ArgvRunner(),
        events=FakeEvents(),
        auth=SimpleNamespace(runtime=SimpleNamespace(whisperx_hf_token="")),
    )

    result = asyncio.run(
        media.detect_person_clip_ranges_with_python(
            tmp_path / "long.mp4",
            600.0,
            options={
                "preset": "solo",
                "minPeople": 1,
                "minBoxHeightRatio": 0.4,
                "minSessionSeconds": 120.0,
            },
        )
    )

    args = recorded["args"]
    for flag, value in (
        ("--preset", "solo"),
        ("--min-people", "1"),
        ("--min-box-height-ratio", "0.4"),
        ("--min-session-seconds", "120.0"),
    ):
        assert flag in args, args
        assert args[args.index(flag) + 1] == value

    # The clip list records the rule the detector reported running, so a run
    # stays auditable after the fact.
    assert result["source"]["preset"] == "solo"
    assert result["source"]["minBoxHeightRatio"] == 0.4
    assert result["source"]["rejectedSmallBoxes"] == 42


def test_segmentation_presets_endpoint_lists_every_preset(tmp_path) -> None:
    """The upload screen renders its picker from this endpoint, so a preset
    added or retuned in the backend needs no frontend change."""
    from app.pipeline import person_presets

    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    response = client.get(
        "/api/settings/segmentation-presets", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert [item["id"] for item in body["presets"]] == list(person_presets.PRESET_IDS)
    assert body["defaultPreset"] == "pair"
    solo = next(item for item in body["presets"] if item["id"] == "solo")
    assert (solo["minPeople"], solo["minBoxHeightRatio"]) == (1, 0.4)
    # Bounds ship with the catalogue so the custom form validates the same
    # range the API enforces instead of hardcoding a second copy.
    assert body["bounds"]["minPeople"] == [1, 10]


def test_initiate_persists_resolved_segmentation_options(tmp_path) -> None:
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    response = client.post(
        "/api/uploads/initiate",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "workflow": "long",
            "autoProcess": True,
            "segmentation": "person",
            "segmentationOptions": {"preset": "solo"},
            "files": [
                {"kind": "video", "originalName": "station.mp4", "mimeType": "video/mp4", "sizeBytes": 5},
                {"kind": "caseStudy", "originalName": "case.pdf", "mimeType": "application/pdf", "sizeBytes": 4},
            ],
        },
    )
    assert response.status_code == 201, response.text
    # Both halves are persisted: the name the operator picked AND the numbers it
    # meant on the day, so a later retune cannot silently re-cut this session.
    assert response.json()["session"]["segmentationOptions"] == {
        "preset": "solo",
        "minPeople": 1,
        "minBoxHeightRatio": 0.4,
        "minSessionSeconds": 120.0,
    }

    stored = asyncio.run(client.app.state.container.sessions.read(response.json()["session"]["id"]))
    assert stored["segmentationOptions"]["minPeople"] == 1


def test_initiate_accepts_custom_occupancy_numbers(tmp_path) -> None:
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    response = client.post(
        "/api/uploads/initiate",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "workflow": "long",
            "autoProcess": True,
            "segmentation": "person",
            "segmentationOptions": {
                "preset": "custom",
                "minPeople": 3,
                "minBoxHeightRatio": 0.55,
                "minSessionSeconds": 45,
            },
            "files": [
                {"kind": "video", "originalName": "station.mp4", "mimeType": "video/mp4", "sizeBytes": 5},
                {"kind": "caseStudy", "originalName": "case.pdf", "mimeType": "application/pdf", "sizeBytes": 4},
            ],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["session"]["segmentationOptions"] == {
        "preset": "custom",
        "minPeople": 3,
        "minBoxHeightRatio": 0.55,
        "minSessionSeconds": 45.0,
    }


def test_initiate_rejects_unknown_preset_and_out_of_range_numbers(tmp_path) -> None:
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    files = [
        {"kind": "video", "originalName": "station.mp4", "mimeType": "video/mp4", "sizeBytes": 5},
        {"kind": "caseStudy", "originalName": "case.pdf", "mimeType": "application/pdf", "sizeBytes": 4},
    ]
    for options in (
        {"preset": "laser-eyes"},
        {"preset": "custom", "minPeople": 99},
        {"preset": "custom", "minBoxHeightRatio": 2.0},
    ):
        response = client.post(
            "/api/uploads/initiate",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "workflow": "long",
                "autoProcess": True,
                "segmentation": "person",
                "segmentationOptions": options,
                "files": files,
            },
        )
        assert response.status_code in {400, 422}, (options, response.text)


def test_initiate_ignores_occupancy_options_for_bell_detection(tmp_path) -> None:
    """The rule only means anything to the person detector; storing it beside a
    bell-split session would claim a setting that never ran."""
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    response = client.post(
        "/api/uploads/initiate",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "workflow": "long",
            "autoProcess": True,
            "segmentation": "bells",
            "segmentationOptions": {"preset": "solo"},
            "files": [
                {"kind": "video", "originalName": "station.mp4", "mimeType": "video/mp4", "sizeBytes": 5},
                {"kind": "caseStudy", "originalName": "case.pdf", "mimeType": "application/pdf", "sizeBytes": 4},
            ],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["session"]["segmentationOptions"] is None


def test_initiate_standard_workflow_drops_segmentation(tmp_path) -> None:
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    response = client.post(
        "/api/uploads/initiate",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "workflow": "standard",
            "autoProcess": True,
            "segmentation": "person",
            "files": [
                {"kind": "video", "originalName": "station.mp4", "mimeType": "video/mp4", "sizeBytes": 5},
                {"kind": "caseStudy", "originalName": "case.pdf", "mimeType": "application/pdf", "sizeBytes": 4},
            ],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["session"]["segmentation"] is None


def test_initiate_rejects_invalid_segmentation(tmp_path) -> None:
    client = build_test_client(tmp_path)
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    response = client.post(
        "/api/uploads/initiate",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "workflow": "long",
            "autoProcess": True,
            "segmentation": "sonar",
            "files": [
                {"kind": "video", "originalName": "station.mp4", "mimeType": "video/mp4", "sizeBytes": 5},
                {"kind": "caseStudy", "originalName": "case.pdf", "mimeType": "application/pdf", "sizeBytes": 4},
            ],
        },
    )
    # Production main.py maps RequestValidationError to 400; the bare test app
    # uses FastAPI's default 422. Either way the invalid value must be rejected.
    assert response.status_code in {400, 422}
