from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from app.pipeline.transcription import registry


class UpdateSettingsRequest(BaseModel):
    # extra="forbid" so an unknown key 422s instead of being silently dropped —
    # a typo'd setting name should be loud, not a no-op.
    model_config = ConfigDict(extra="forbid")

    llmTranscriptPreprocess: bool
    # "" means "use this deployment's configured default engine".
    transcriptionEngine: str = ""
    # Option overrides per engine id: {"whisperx": {"model": "large-v3"}}.
    # Keeping them per engine means switching engines and switching back does
    # not discard the tuning done for either one.
    transcriptionEngineOptions: dict[str, dict[str, Any]] = {}

    @field_validator("transcriptionEngine")
    @classmethod
    def known_engine(cls, value: str) -> str:
        engine_id = str(value or "").strip()
        if engine_id and engine_id not in registry.ENGINE_FACTORIES:
            known = ", ".join(registry.engine_ids())
            raise ValueError(f"Unknown transcription engine '{engine_id}'. Available: {known}.")
        return engine_id

    @field_validator("transcriptionEngineOptions")
    @classmethod
    def valid_options(cls, value: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Validate every engine's options against that engine's own schema.

        Done at the API boundary so an unusable value is rejected while the
        operator is looking at the form, rather than failing a transcription
        an hour later.
        """
        from app.pipeline.transcription.base import TranscriptionOptionError, validate_options

        validated: dict[str, dict[str, Any]] = {}
        for engine_id, options in (value or {}).items():
            descriptor = registry.descriptor_for(str(engine_id))
            if descriptor is None:
                known = ", ".join(registry.engine_ids())
                raise ValueError(f"Unknown transcription engine '{engine_id}'. Available: {known}.")
            try:
                validated[str(engine_id)] = validate_options(descriptor, options)
            except TranscriptionOptionError as error:
                # Re-raised as a ValueError so it lands in the request's
                # validation report next to the field it came from.
                raise ValueError(error.message) from None
        return validated
