"""The router: which engine runs, with which options, and what happens when
the stored selection no longer makes sense.

The selection is read live from the database on every run, so these tests pin
the failure modes that would otherwise silently change what a run produces: an
engine id this build no longer ships, options a later release narrowed, and a
database that cannot be read at all.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.pipeline.media import MediaPipeline
from app.pipeline.transcription import registry
from app.pipeline.transcription.base import (
    EngineAvailability,
    EngineCapabilities,
    EngineDescriptor,
    ParameterSpec,
    ParameterType,
    PrefetchResult,
    TranscriptionEngine,
    TranscriptionRequest,
    TranscriptionResult,
)
from app.services.transcription_router import TranscriptionRouter

from tests.test_pipeline_service import FakeEvents


class RecordingEngine(TranscriptionEngine):
    """Stands in for a real engine; records the request it was handed."""

    descriptor = EngineDescriptor(
        id="recording",
        label="Recording Engine",
        vendor="Tests",
        description="Records requests.",
        capabilities=EngineCapabilities(diarization=False),
        parameters=(
            ParameterSpec(
                name="batchSize",
                label="Batch size",
                type=ParameterType.INTEGER,
                default=1,
                minimum=1,
                maximum=8,
            ),
        ),
    )

    def __init__(self, diarized: bool = False) -> None:
        self.requests: list[TranscriptionRequest] = []
        self.diarized = diarized

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        self.requests.append(request)
        return TranscriptionResult(
            json_path=Path("transcript.json"),
            srt_path=None,
            vtt_path=None,
            engine_id=self.descriptor.id,
            model="recording-model",
            diarized=self.diarized,
        )


class StubAppSettings:
    def __init__(self, engine_id: str = "", options: dict[str, Any] | None = None, error: bool = False) -> None:
        self.engine_id = engine_id
        self.options = options or {}
        self.error = error

    async def transcription_selection(self, user_id: str | None = None) -> tuple[str, dict[str, Any]]:
        """Mirrors AppSettingsRepository: the stored id plus the options map
        keyed by engine id. The map is deliberately not pre-resolved — picking
        the bag is the router's job, because only it knows which engine an empty
        or unknown stored id falls back to."""
        if self.error:
            raise RuntimeError("database unavailable")
        options_by_engine = {self.engine_id: self.options} if self.engine_id else {}
        return self.engine_id, options_by_engine


def build_router(
    tmp_path: Path,
    app_settings: StubAppSettings | None = None,
    **setting_overrides: Any,
) -> tuple[TranscriptionRouter, FakeEvents]:
    settings = Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        **setting_overrides,
    )
    events = FakeEvents()

    class NullRunner:
        async def run(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("the router must not run commands itself")

    runner = NullRunner()
    auth = type("Auth", (), {"runtime": type("Runtime", (), {"whisperx_hf_token": "hf"})()})()
    media = MediaPipeline(settings, runner, events, auth)
    dependencies = registry.EngineDependencies(settings, runner, events, auth, media)
    return TranscriptionRouter(settings, events, dependencies, preferences=app_settings), events


def run_with_recording_engine(
    tmp_path: Path,
    app_settings: StubAppSettings,
    *,
    diarized: bool = False,
    session: dict[str, Any] | None = None,
) -> tuple[RecordingEngine, TranscriptionResult, FakeEvents]:
    router, events = build_router(tmp_path, app_settings)
    engine = RecordingEngine(diarized=diarized)
    router.engines["recording"] = engine
    result = asyncio.run(
        router.transcribe(
            session or {"id": "session-1"},
            {"fileName": "session-1.mp3", "absolutePath": str(tmp_path / "session-1.mp3")},
        )
    )
    return engine, result, events


def test_no_stored_selection_uses_the_deployment_default(tmp_path: Path) -> None:
    router, _ = build_router(tmp_path, StubAppSettings())

    engine_id, options = asyncio.run(router.selection())

    assert engine_id == "whisperx"
    assert options == {}


def test_the_deployment_default_is_configurable(tmp_path: Path) -> None:
    router, _ = build_router(tmp_path, StubAppSettings(), transcription_engine="canary-qwen")

    assert router.default_engine_id() == "canary-qwen"


def test_an_unconfigurable_default_falls_back_to_whisperx(tmp_path: Path) -> None:
    # A typo in the environment must not leave the deployment with no engine.
    router, _ = build_router(tmp_path, StubAppSettings(), transcription_engine="not-an-engine")

    assert router.default_engine_id() == registry.DEFAULT_ENGINE_ID


def test_a_stored_selection_for_a_removed_engine_falls_back(tmp_path: Path) -> None:
    router, _ = build_router(tmp_path, StubAppSettings(engine_id="retired-engine"))

    engine_id, _ = asyncio.run(router.selection())

    assert engine_id == "whisperx"


def test_a_settings_read_failure_falls_back_rather_than_failing_the_run(tmp_path: Path) -> None:
    router, _ = build_router(tmp_path, StubAppSettings(error=True))

    engine_id, options = asyncio.run(router.selection())

    assert engine_id == "whisperx"
    assert options == {}


def test_the_selected_engine_receives_the_resolved_options(tmp_path: Path) -> None:
    engine, _, _ = run_with_recording_engine(
        tmp_path, StubAppSettings(engine_id="recording", options={"batchSize": 4})
    )

    assert engine.requests[0].options["batchSize"] == 4


def test_invalid_stored_options_fall_back_to_engine_defaults(tmp_path: Path) -> None:
    # An option a later release narrowed must not strand a deployment on a
    # transcription it can no longer start.
    engine, _, _ = run_with_recording_engine(
        tmp_path, StubAppSettings(engine_id="recording", options={"batchSize": 999})
    )

    assert engine.requests[0].options["batchSize"] == 1


def test_unknown_stored_options_fall_back_to_engine_defaults(tmp_path: Path) -> None:
    engine, _, _ = run_with_recording_engine(
        tmp_path, StubAppSettings(engine_id="recording", options={"removedOption": 3})
    )

    assert engine.requests[0].options == {"batchSize": 1}


def test_the_request_carries_the_session_context_every_engine_needs(tmp_path: Path) -> None:
    session = {"id": "session-1", "corpus": {"terms": ["paracetamol"]}}
    engine, _, _ = run_with_recording_engine(
        tmp_path, StubAppSettings(engine_id="recording"), session=session
    )

    request = engine.requests[0]
    assert request.session_id == "session-1"
    assert request.corpus_terms == ["paracetamol"]
    assert request.min_speakers == 2 and request.max_speakers == 2
    assert request.output_base_name == "session-1"
    assert request.output_dir.name == "session-1"


def test_an_undiarized_result_is_announced_loudly(tmp_path: Path) -> None:
    # Unlabelled dialogue changes what the scorers can conclude, so it is never
    # a silent quality regression.
    _, result, events = run_with_recording_engine(tmp_path, StubAppSettings(engine_id="recording"))

    messages = [str(payload.get("message", "")) for _, _, payload in events.items]
    assert result.diarized is False
    assert any("no speaker labels" in message for message in messages)


def test_a_diarized_result_raises_no_warning(tmp_path: Path) -> None:
    _, _, events = run_with_recording_engine(
        tmp_path, StubAppSettings(engine_id="recording"), diarized=True
    )

    messages = [str(payload.get("message", "")) for _, _, payload in events.items]
    assert not any("no speaker labels" in message for message in messages)
    assert any("Transcribing with" in message for message in messages)


def test_describe_lists_every_engine_with_its_availability(tmp_path: Path) -> None:
    router, _ = build_router(tmp_path, StubAppSettings(engine_id="canary-qwen"))

    async def unavailable() -> EngineAvailability:
        return EngineAvailability(False, "not installed")

    router.engines["canary-qwen"].availability = unavailable  # type: ignore[method-assign]
    description = asyncio.run(router.describe())

    engines = {engine["id"]: engine for engine in description["engines"]}
    assert set(engines) == set(registry.engine_ids())
    assert engines["canary-qwen"]["availability"] == {"available": False, "reason": "not installed"}
    assert engines["whisperx"]["defaults"]["model"]
    assert description["selected"]["engineId"] == "canary-qwen"
    assert description["defaultEngineId"] == "whisperx"


def test_result_metadata_records_provenance(tmp_path: Path) -> None:
    router, _ = build_router(tmp_path, StubAppSettings())
    result = TranscriptionResult(
        json_path=Path("t.json"),
        srt_path=None,
        vtt_path=None,
        engine_id="whisperx",
        model="large-v3",
        diarized=True,
        metadata={"chunkSize": 20},
    )

    described = router.describe_result(result)

    assert described["engineId"] == "whisperx"
    assert described["engineLabel"] == "WhisperX"
    assert described["model"] == "large-v3"
    assert described["diarized"] is True
    assert described["chunkSize"] == 20


class PrefetchEngine(RecordingEngine):
    """Records whether startup asked it to cache its weights."""

    def __init__(self, ready: bool = True) -> None:
        super().__init__()
        self.ready = ready
        self.prefetch_calls = 0

    async def prefetch(self) -> PrefetchResult:
        self.prefetch_calls += 1
        if self.ready:
            return PrefetchResult(True, "cached")
        raise RuntimeError("the network is down")


def test_startup_prefetches_the_selected_engine(tmp_path: Path) -> None:
    router, _ = build_router(tmp_path, StubAppSettings(engine_id="recording"))
    engine = PrefetchEngine()
    router.engines["recording"] = engine

    asyncio.run(router.prefetch_selected_engine())

    assert engine.prefetch_calls == 1


def test_a_prefetch_failure_does_not_break_startup(tmp_path: Path) -> None:
    router, _ = build_router(tmp_path, StubAppSettings(engine_id="recording"))
    router.engines["recording"] = PrefetchEngine(ready=False)

    asyncio.run(router.prefetch_selected_engine())  # must not raise


def test_prefetch_can_be_turned_off(tmp_path: Path) -> None:
    # An air-gapped or metered host downloads on demand instead.
    router, _ = build_router(
        tmp_path, StubAppSettings(engine_id="recording"), transcription_prefetch_models=False
    )
    engine = PrefetchEngine()
    router.engines["recording"] = engine

    asyncio.run(router.prefetch_selected_engine())

    assert engine.prefetch_calls == 0
