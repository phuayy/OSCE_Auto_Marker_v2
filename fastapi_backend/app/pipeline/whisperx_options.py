"""Per-run WhisperX settings.

The WhisperX invocation used to read every knob straight off the global
``Settings`` object. With an operator-selectable engine, the same process can
be asked to run two sessions with different models, so the knobs travel with
the call instead. ``from_settings`` keeps the environment variables as the
deployment-wide baseline: a run that passes no options behaves exactly as it
did before.

Frozen because it is shared across the reader threads that stream a run's
output; nothing may mutate a run's configuration once it has started.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, typing only
    from app.core.config import Settings


@dataclass(frozen=True)
class WhisperxRunOptions:
    model: str
    compute_type: str
    batch_size: int
    chunk_size: int
    min_speakers: int
    max_speakers: int
    audio_filters: str
    initial_prompt: str
    language: str
    output_format: str
    print_progress: bool

    @classmethod
    def from_settings(cls, settings: "Settings") -> "WhisperxRunOptions":
        return cls(
            model=settings.whisperx_model,
            compute_type=settings.whisperx_compute_type,
            batch_size=settings.whisperx_batch_size,
            chunk_size=settings.whisperx_chunk_size,
            min_speakers=settings.whisperx_min_speakers,
            max_speakers=settings.whisperx_max_speakers,
            audio_filters=settings.whisperx_audio_filters,
            initial_prompt=settings.whisperx_initial_prompt,
            language=settings.whisperx_language,
            output_format=settings.whisperx_output_format,
            print_progress=settings.whisperx_print_progress,
        )

    def with_overrides(self, **overrides: Any) -> "WhisperxRunOptions":
        """A copy with the non-``None`` overrides applied; ``None`` means
        "keep the deployment default", which is how an unset engine option and
        an explicitly chosen one are told apart."""
        return replace(self, **{key: value for key, value in overrides.items() if value is not None})
