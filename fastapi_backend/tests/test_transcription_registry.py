"""The engine registry — the one place that enumerates engines.

The settings API, the option validation and the router all read it, so these
tests pin the invariants a new engine has to satisfy: a unique id, a schema
whose own defaults validate, and defaults that actually come from ``Settings``
rather than being hardcoded twice.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.core.config import Settings
from app.pipeline.media import MediaPipeline
from app.pipeline.transcription import registry
from app.pipeline.transcription.base import TranscriptionEngine, validate_options


def build_dependencies(tmp_path: Path, **overrides) -> registry.EngineDependencies:
    settings = Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        **overrides,
    )
    runner = SimpleNamespace(run=None)
    events = SimpleNamespace(publish=None)
    auth = SimpleNamespace(runtime=SimpleNamespace(whisperx_hf_token="hf-token"))
    media = MediaPipeline(settings, runner, events, auth)
    return registry.EngineDependencies(settings, runner, events, auth, media)


def test_the_registry_ships_the_expected_engines(tmp_path: Path) -> None:
    assert registry.engine_ids() == ["whisperx", "canary-qwen"]
    assert registry.DEFAULT_ENGINE_ID == "whisperx"


def test_every_engine_has_a_descriptor_matching_its_id(tmp_path: Path) -> None:
    for engine_id in registry.engine_ids():
        descriptor = registry.descriptor_for(engine_id)
        assert descriptor is not None
        assert descriptor.id == engine_id
        assert descriptor.label and descriptor.description


def test_build_all_constructs_every_engine(tmp_path: Path) -> None:
    engines = registry.build_all(build_dependencies(tmp_path))

    assert set(engines) == set(registry.engine_ids())
    assert all(isinstance(engine, TranscriptionEngine) for engine in engines.values())


def test_engine_defaults_validate_against_their_own_schema(tmp_path: Path) -> None:
    # A default the schema would reject makes every run that omits an option
    # fail, so this is the invariant a new engine most needs held to.
    for engine in registry.build_all(build_dependencies(tmp_path)).values():
        validate_options(engine.descriptor, engine.default_options())


def test_defaults_track_the_environment_settings(tmp_path: Path) -> None:
    dependencies = build_dependencies(
        tmp_path,
        whisperx_model="distil-large-v3",
        whisperx_chunk_size=15,
        canary_chunk_seconds=25.0,
    )
    engines = registry.build_all(dependencies)

    assert engines["whisperx"].default_options()["model"] == "distil-large-v3"
    assert engines["whisperx"].default_options()["chunkSize"] == 15
    assert engines["canary-qwen"].default_options()["chunkSeconds"] == 25.0


def test_resolve_options_layers_overrides_on_deployment_defaults(tmp_path: Path) -> None:
    engine = registry.build_all(build_dependencies(tmp_path, whisperx_batch_size=4))["whisperx"]

    resolved = engine.resolve_options({"model": "large-v3-turbo"})

    assert resolved["model"] == "large-v3-turbo"
    assert resolved["batchSize"] == 4  # untouched by the override


def test_only_whisperx_claims_to_diarize(tmp_path: Path) -> None:
    # The router adds a diarisation pass for the others; a wrong capability
    # flag would either skip that pass or run it redundantly.
    engines = registry.build_all(build_dependencies(tmp_path))

    assert engines["whisperx"].descriptor.capabilities.diarization is True
    assert engines["canary-qwen"].descriptor.capabilities.diarization is False


def test_build_engine_rejects_an_unknown_id(tmp_path: Path) -> None:
    dependencies = build_dependencies(tmp_path)

    try:
        registry.build_engine("does-not-exist", dependencies)
    except KeyError:
        return
    raise AssertionError("an unknown engine id must not resolve")
