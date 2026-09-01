"""The set of transcription engines this build knows about.

Adding an engine is: write the module, add one line here. Nothing else in the
backend enumerates engines — the settings API, the option validation and the
router all read this registry, so a new entry appears in the UI automatically.
"""
from __future__ import annotations

from collections.abc import Callable

from app.core.config import Settings
from app.core.process import CommandRunner
from app.pipeline.media import MediaPipeline
from app.pipeline.transcription import canary_qwen_engine, whisperx_engine
from app.pipeline.transcription.base import EngineDescriptor, TranscriptionEngine
from app.pipeline.transcription.diarization import PyannoteDiarizer
from app.services.auth_service import AuthService
from app.services.event_service import EventService

# The engine used when nothing is selected, when a stored selection names an
# engine this build no longer ships, or when the selected engine cannot run.
# It is the only engine that needs no companion diarisation pass.
DEFAULT_ENGINE_ID = whisperx_engine.ENGINE_ID


class EngineDependencies:
    """Everything the engine constructors draw on, assembled once at startup."""

    def __init__(
        self,
        settings: Settings,
        runner: CommandRunner,
        events: EventService,
        auth: AuthService,
        media: MediaPipeline,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.events = events
        self.auth = auth
        self.media = media
        self.diarizer = PyannoteDiarizer(settings, runner, events, auth)


EngineFactory = Callable[[EngineDependencies], TranscriptionEngine]


def _build_whisperx(dependencies: EngineDependencies) -> TranscriptionEngine:
    return whisperx_engine.WhisperXEngine(dependencies.media)


def _build_canary_qwen(dependencies: EngineDependencies) -> TranscriptionEngine:
    return canary_qwen_engine.CanaryQwenEngine(
        dependencies.settings,
        dependencies.runner,
        dependencies.events,
        dependencies.media,
        dependencies.diarizer,
    )


# Ordered: the UI lists engines in this order, most-supported first.
ENGINE_FACTORIES: dict[str, EngineFactory] = {
    whisperx_engine.ENGINE_ID: _build_whisperx,
    canary_qwen_engine.ENGINE_ID: _build_canary_qwen,
}

DESCRIPTORS: dict[str, EngineDescriptor] = {
    whisperx_engine.ENGINE_ID: whisperx_engine.DESCRIPTOR,
    canary_qwen_engine.ENGINE_ID: canary_qwen_engine.DESCRIPTOR,
}


def engine_ids() -> list[str]:
    return list(ENGINE_FACTORIES)


def descriptor_for(engine_id: str) -> EngineDescriptor | None:
    return DESCRIPTORS.get(str(engine_id))


def build_engine(engine_id: str, dependencies: EngineDependencies) -> TranscriptionEngine:
    """Instantiate one engine. Raises ``KeyError`` for an unknown id — callers
    that accept operator input resolve the id against the registry first."""
    return ENGINE_FACTORIES[str(engine_id)](dependencies)


def build_all(dependencies: EngineDependencies) -> dict[str, TranscriptionEngine]:
    """Instantiate every engine. Construction is cheap — no model loads, no
    subprocesses — so the router holds them all and never rebuilds per run."""
    return {engine_id: factory(dependencies) for engine_id, factory in ENGINE_FACTORIES.items()}
