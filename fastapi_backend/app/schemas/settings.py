from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from app.llm import registry as llm_registry
from app.pipeline.transcription import registry


class LLMTargetPayload(BaseModel):
    """One provider/model pair from the settings screen's dropdowns.

    An empty ``providerId`` is how the UI says "no fallback"; the router treats
    such an entry as absent rather than as an error, so clearing the fallback
    dropdown is a normal save and not a validation failure.
    """

    model_config = ConfigDict(extra="forbid")

    providerId: str = ""
    # Free text on purpose: vendors ship models faster than this repo is
    # released, and refusing an id we have not catalogued would make the
    # product stale the week after a launch. An unknown id fails loudly at the
    # provider with its own error message.
    model: str = ""

    @field_validator("providerId")
    @classmethod
    def known_provider(cls, value: str) -> str:
        provider_id = str(value or "").strip()
        if provider_id and provider_id not in llm_registry.PROVIDER_FACTORIES:
            known = ", ".join(llm_registry.provider_ids())
            raise ValueError(f"Unknown LLM provider '{provider_id}'. Available: {known}.")
        return provider_id

    @field_validator("model")
    @classmethod
    def trimmed_model(cls, value: str) -> str:
        return str(value or "").strip()


class TestLLMTargetRequest(BaseModel):
    """Body of the settings screen's "Test" button.

    Separate from the settings update so an operator can probe a provider
    before committing to it — testing must not be a side effect of saving.
    """

    model_config = ConfigDict(extra="forbid")

    providerId: str
    model: str = ""

    @field_validator("providerId")
    @classmethod
    def known_provider(cls, value: str) -> str:
        provider_id = str(value or "").strip()
        if not provider_id:
            raise ValueError("providerId is required.")
        if provider_id not in llm_registry.PROVIDER_FACTORIES:
            known = ", ".join(llm_registry.provider_ids())
            raise ValueError(f"Unknown LLM provider '{provider_id}'. Available: {known}.")
        return provider_id


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
    # Scoring model routing. An empty primary means "this deployment's default
    # provider"; fallbacks are tried in order when the primary fails.
    llmPrimary: LLMTargetPayload = LLMTargetPayload()
    llmFallbacks: list[LLMTargetPayload] = []

    @field_validator("llmFallbacks")
    @classmethod
    def usable_fallbacks(cls, value: list[LLMTargetPayload]) -> list[LLMTargetPayload]:
        """Drop blank rows so "no fallback" round-trips as an empty list.

        The UI sends a placeholder entry when its fallback dropdown is on
        "None". Storing that placeholder would leave a target with no provider
        for the router to skip on every single call.
        """
        return [target for target in (value or []) if target.providerId]

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
