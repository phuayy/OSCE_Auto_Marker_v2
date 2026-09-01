"""Contracts every transcription engine implements.

The pipeline used to call the WhisperX CLI directly. It now asks a *router* for
whichever engine the operator selected, so a second ASR system is a new module
plus a registry entry rather than a change to the pipeline. The contract is
deliberately narrow: an engine is handed one audio file and a validated option
bag, and returns the three artifacts the rest of the pipeline already consumes
(a WhisperX-shaped JSON transcript, an SRT and a VTT).

Engines differ in what they can do — Canary-Qwen produces text with no speaker
labels at all, WhisperX diarises and aligns — so each declares its
capabilities and the router fills the gaps rather than every caller
special-casing engines.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from app.core.exceptions import AppError

# Called with the transcription step's completion percentage (0-100) whenever it
# advances. Awaited, so a handler may persist the value.
ProgressCallback = Callable[[float], Awaitable[None]]


class ParameterType(str, Enum):
    INTEGER = "int"
    FLOAT = "float"
    BOOLEAN = "bool"
    STRING = "string"
    ENUM = "enum"


class TranscriptionOptionError(AppError):
    """An engine option the operator sent is unknown or out of range.

    A 4xx rather than a 5xx: the request is wrong, and retrying it unchanged
    (as the job queue would for a transport error) can never help.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=422)


@dataclass(frozen=True)
class ParameterSpec:
    """One operator-tunable engine setting, and everything the UI needs to draw
    a control for it without knowing which engine it belongs to."""

    name: str
    label: str
    type: ParameterType
    default: Any
    help: str = ""
    minimum: float | None = None
    maximum: float | None = None
    options: tuple[str, ...] = ()
    # Advanced parameters are collapsed in the UI: correct defaults matter more
    # than discoverability for these.
    advanced: bool = False

    def coerce(self, value: Any) -> Any:
        """Validate and convert one submitted value, or raise.

        Values arrive from JSON, where a number may be a string and a boolean
        may be "true" — coercing here keeps every engine free of parsing code.
        """
        if value is None:
            return self.default
        try:
            coerced = self._convert(value)
        except (TypeError, ValueError):
            raise TranscriptionOptionError(
                f"'{self.label}' expects a {self.type.value} value, got {value!r}."
            ) from None
        return self._check_range(coerced)

    def _convert(self, value: Any) -> Any:
        if self.type is ParameterType.INTEGER:
            return int(str(value).strip()) if isinstance(value, str) else int(value)
        if self.type is ParameterType.FLOAT:
            return float(str(value).strip()) if isinstance(value, str) else float(value)
        if self.type is ParameterType.BOOLEAN:
            if isinstance(value, bool):
                return value
            normalized = str(value).strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off"}:
                return False
            raise ValueError(normalized)
        text = str(value).strip()
        if self.type is ParameterType.ENUM and text not in self.options:
            raise TranscriptionOptionError(
                f"'{self.label}' must be one of {', '.join(self.options)}; got {text!r}."
            )
        return text

    def _check_range(self, value: Any) -> Any:
        if self.type not in {ParameterType.INTEGER, ParameterType.FLOAT}:
            return value
        if self.minimum is not None and value < self.minimum:
            raise TranscriptionOptionError(f"'{self.label}' must be at least {self.minimum}.")
        if self.maximum is not None and value > self.maximum:
            raise TranscriptionOptionError(f"'{self.label}' must be at most {self.maximum}.")
        return value

    def to_public(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "label": self.label,
            "type": self.type.value,
            "default": self.default,
            "help": self.help,
            "advanced": self.advanced,
        }
        if self.minimum is not None:
            payload["minimum"] = self.minimum
        if self.maximum is not None:
            payload["maximum"] = self.maximum
        if self.options:
            payload["options"] = list(self.options)
        return payload


@dataclass(frozen=True)
class EngineCapabilities:
    """What an engine does for itself, and therefore what the router must add.

    ``diarization`` is the consequential one: the scorers read speaker-tagged
    dialogue, so an engine that cannot label speakers is run with a separate
    diarisation pass rather than being allowed to emit an unlabelled transcript.
    """

    diarization: bool = False
    word_timestamps: bool = False
    # True when the engine writes its own SRT/VTT; otherwise the router renders
    # subtitles from the returned segments.
    subtitles: bool = False
    # True when the engine reports progress it can stream to the session card.
    progress: bool = False

    def to_public(self) -> dict[str, bool]:
        return {
            "diarization": self.diarization,
            "wordTimestamps": self.word_timestamps,
            "subtitles": self.subtitles,
            "progress": self.progress,
        }


@dataclass(frozen=True)
class EngineDescriptor:
    """The engine's identity and contract, as shown in the settings UI."""

    id: str
    label: str
    vendor: str
    description: str
    capabilities: EngineCapabilities
    parameters: tuple[ParameterSpec, ...] = ()
    # Human-readable install/hardware note shown when the engine is unavailable.
    requirements: str = ""

    def parameter(self, name: str) -> ParameterSpec | None:
        return next((spec for spec in self.parameters if spec.name == name), None)

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "vendor": self.vendor,
            "description": self.description,
            "capabilities": self.capabilities.to_public(),
            "parameters": [spec.to_public() for spec in self.parameters],
            "requirements": self.requirements,
        }


@dataclass(frozen=True)
class EngineAvailability:
    """Whether this machine can actually run the engine right now."""

    available: bool
    reason: str = ""

    def to_public(self) -> dict[str, Any]:
        return {"available": self.available, "reason": self.reason}


@dataclass
class TranscriptionRequest:
    """Everything an engine needs for one session's audio.

    The session dict is deliberately absent: engines receive resolved values so
    they cannot reach into session state, and so a request can be constructed
    in a test without a session document.
    """

    session_id: str
    # The extracted MP3. Engines that need different audio derive their own
    # file — the MP3 is measured by the audio-professionalism scorer and must
    # never be modified in place.
    audio_path: Path
    audio_file_name: str
    output_dir: Path
    language: str
    options: dict[str, Any] = field(default_factory=dict)
    # The session's snapshotted corpus terms. Each engine decides how to apply
    # them — WhisperX biases decoding with --hotwords; an engine without a
    # biasing mechanism ignores them and still benefits from the deterministic
    # corpus correction the pipeline applies afterwards.
    corpus_terms: list[str] = field(default_factory=list)
    min_speakers: int = 0
    max_speakers: int = 0
    on_progress: ProgressCallback | None = None

    @property
    def output_base_name(self) -> str:
        """Artifact stem. Shared by every engine so the artifact cache, the
        media URLs and the subtitle lookup keep working across a switch."""
        return Path(self.audio_file_name).stem


@dataclass
class TranscriptionResult:
    """Artifacts produced for one session, plus how they were produced."""

    json_path: Path
    srt_path: Path | None
    vtt_path: Path | None
    engine_id: str
    model: str = ""
    diarized: bool = False
    # Free-form, engine-specific detail recorded on the pipeline step.
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_outputs(self) -> dict[str, Path | None]:
        """The shape the pipeline has always consumed from transcription."""
        return {
            "jsonAbsolutePath": self.json_path,
            "srtAbsolutePath": self.srt_path,
            "vttAbsolutePath": self.vtt_path,
        }


class TranscriptionEngine:
    """Base class for engines. Subclasses supply a descriptor and ``transcribe``."""

    descriptor: EngineDescriptor

    async def availability(self) -> EngineAvailability:
        """Whether the engine can run here. Never raises: an engine that cannot
        answer is reported unavailable with the reason, because this feeds a
        settings screen, not a pipeline run."""
        return EngineAvailability(True)

    def default_options(self) -> dict[str, Any]:
        """Descriptor defaults, which subclasses override from ``Settings`` so
        the existing environment variables stay the deployment-wide baseline."""
        return {spec.name: spec.default for spec in self.descriptor.parameters}

    def resolve_options(self, overrides: dict[str, Any] | None) -> dict[str, Any]:
        """Operator overrides layered over this deployment's defaults."""
        resolved = dict(self.default_options())
        resolved.update(validate_options(self.descriptor, overrides))
        return resolved

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        raise NotImplementedError


def validate_options(descriptor: EngineDescriptor, raw: dict[str, Any] | None) -> dict[str, Any]:
    """Coerce a submitted option bag against an engine's parameter schema.

    Unknown keys are refused rather than ignored: a typo'd option that silently
    does nothing is indistinguishable from one that did not take effect.
    """
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise TranscriptionOptionError("Engine options must be an object.")
    validated: dict[str, Any] = {}
    for name, value in raw.items():
        spec = descriptor.parameter(str(name))
        if spec is None:
            known = ", ".join(spec.name for spec in descriptor.parameters) or "none"
            raise TranscriptionOptionError(
                f"'{name}' is not an option of {descriptor.label}. Known options: {known}."
            )
        validated[spec.name] = spec.coerce(value)
    return validated
