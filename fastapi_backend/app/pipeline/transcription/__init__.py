"""Pluggable transcription engines.

``base`` defines the contract, ``registry`` lists the engines this build ships,
and ``app.services.transcription_router`` picks one per run from the operator's
saved selection.
"""
from app.pipeline.transcription.base import (
    EngineAvailability,
    EngineCapabilities,
    EngineDescriptor,
    ParameterSpec,
    ParameterType,
    TranscriptionEngine,
    TranscriptionOptionError,
    TranscriptionRequest,
    TranscriptionResult,
    validate_options,
)

__all__ = [
    "EngineAvailability",
    "EngineCapabilities",
    "EngineDescriptor",
    "ParameterSpec",
    "ParameterType",
    "TranscriptionEngine",
    "TranscriptionOptionError",
    "TranscriptionRequest",
    "TranscriptionResult",
    "validate_options",
]
