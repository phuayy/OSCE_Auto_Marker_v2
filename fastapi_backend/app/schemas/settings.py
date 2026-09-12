"""Request bodies for the settings screen.

One rule shapes every LLM-provider payload here: **which providers exist is no
longer a build-time fact**, so it cannot be checked by a Pydantic validator.
An operator-defined provider lives in the database, and a validator is a
synchronous function with no container, no session and no await. Checking a
provider id here would therefore mean checking it against the six this build
ships and rejecting every custom one.

So these models validate *shape* — trimming, required-ness, the option schema a
transcription engine publishes — and the route handlers validate *existence*
against the live catalogue, where the answer actually lives. The status code is
unchanged (422), so the boundary still behaves the same way from outside.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.llm.panel import MarkingMode, TieBreak
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
    def trimmed_provider(cls, value: str) -> str:
        # Existence is checked in the route against this deployment's catalogue;
        # see the module docstring.
        return str(value or "").strip()

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
    # An unsaved key to probe. Held for the one call and never persisted, so a
    # mistyped credential is caught before it overwrites a working one. Omitted
    # (the normal case), the test uses whatever the server would actually use
    # for a scoring run.
    apiKey: str = ""

    @field_validator("providerId")
    @classmethod
    def present_provider(cls, value: str) -> str:
        provider_id = str(value or "").strip()
        if not provider_id:
            raise ValueError("providerId is required.")
        # An id that no provider answers to is reported by the test itself, as a
        # 200 carrying ok=false: the screen renders a failed probe as a result,
        # and "that provider does not exist" is the most useful result of all.
        return provider_id

    @field_validator("apiKey")
    @classmethod
    def trimmed_key(cls, value: str) -> str:
        return str(value or "").strip()


class SetProviderKeyRequest(BaseModel):
    """Body of the API-key field in the credentials card.

    Only the key. The provider is a path parameter so the value never travels in
    a position where it could be confused for one, and there is deliberately no
    GET counterpart: this endpoint writes credentials, nothing reads them back.

    Format is not validated against a vendor's prefix on purpose. Vendors change
    their key formats, and rejecting a valid new-format key would leave an
    operator unable to configure a working provider; the connection test is the
    check that actually proves a key.
    """

    model_config = ConfigDict(extra="forbid")

    apiKey: str

    @field_validator("apiKey")
    @classmethod
    def present(cls, value: str) -> str:
        key = str(value or "").strip()
        if not key:
            raise ValueError("apiKey is required.")
        return key


class PanelPayload(BaseModel):
    """The multi-model marking panel as the settings screen submits it.

    Shape only, like every other payload here: the markers exist as
    provider/model pairs, the tie-break is one of the known policies. Whether
    the panel is *coherent* — enough markers, no duplicates, an adjudicator —
    is ``PanelConfig.validate()``'s question, asked by the route only when the
    mode is ``panel``: an operator must be able to save a half-built panel
    while single mode is selected.
    """

    model_config = ConfigDict(extra="forbid")

    markers: list[LLMTargetPayload] = []
    adjudicator: LLMTargetPayload = LLMTargetPayload()
    tieBreak: str = str(TieBreak.LENIENT)

    @field_validator("markers")
    @classmethod
    def usable_markers(cls, value: list[LLMTargetPayload]) -> list[LLMTargetPayload]:
        # Same rule as fallbacks: a blank dropdown row is "no marker", not an
        # error, and is not stored.
        return [target for target in (value or []) if target.providerId]

    @field_validator("tieBreak")
    @classmethod
    def known_tie_break(cls, value: str) -> str:
        token = str(value or "").strip().lower() or str(TieBreak.LENIENT)
        try:
            return str(TieBreak(token))
        except ValueError:
            known = ", ".join(str(policy) for policy in TieBreak)
            raise ValueError(f"Unknown tie-break policy '{token}'. Available: {known}.") from None


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
    # How content is marked. "single" is the routing above; "panel" runs the
    # markers in llmPanel and settles their disagreements with its adjudicator.
    llmMarkingMode: str = str(MarkingMode.SINGLE)
    llmPanel: PanelPayload = PanelPayload()

    @field_validator("llmMarkingMode")
    @classmethod
    def known_marking_mode(cls, value: str) -> str:
        token = str(value or "").strip().lower() or str(MarkingMode.SINGLE)
        try:
            return str(MarkingMode(token))
        except ValueError:
            known = ", ".join(str(mode) for mode in MarkingMode)
            raise ValueError(f"Unknown marking mode '{token}'. Available: {known}.") from None

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


class CustomProviderRequest(BaseModel):
    """A scoring provider an operator defines, rather than one this build ships.

    **Everything is optional except the identity, the endpoint and (separately)
    the API key.** The field list is a union across what the current market
    needs to open a connection - bearer tokens, ``x-api-key``, Azure's
    ``api-key`` plus ``api-version``, query-parameter keys, organisation and
    project ids, account ids and regions baked into URLs, gateway headers,
    vendor-specific body switches - because no single vendor needs more than a
    handful of them and there is no useful profile that covers them all. The
    three that are required are not preferences: a provider with no endpoint has
    nothing to call, and one with no id cannot be selected.

    **The model id is deliberately absent.** One key authorises a whole
    catalogue, and the checkpoint changes far more often than the endpoint does,
    so the model stays in Settings -> Scoring model where it already is. This
    payload answers only "how do I talk to this platform".

    Shape only is enforced here; the semantic rules (which auth scheme needs
    which field, what a base URL may be, header-injection safety) live in
    ``app/llm/custom.py`` so the HTTP route, the subprocess loader and any
    future importer enforce one contract rather than three approximations.
    """

    model_config = ConfigDict(extra="forbid")

    # --- identity ---------------------------------------------------------
    id: str = ""
    label: str = ""
    vendor: str = ""
    description: str = ""
    documentationUrl: str = ""

    # --- endpoint ---------------------------------------------------------
    baseUrl: str = ""
    apiFormat: str = "openai"

    # --- authentication ---------------------------------------------------
    authScheme: str = "bearer"
    authHeaderName: str = ""
    authValuePrefix: str = ""
    authQueryParam: str = ""

    # --- deployment identifiers -------------------------------------------
    apiVersion: str = ""
    apiVersionHeader: str = ""
    apiVersionQueryParam: str = ""
    organizationId: str = ""
    organizationHeader: str = ""
    projectId: str = ""
    projectHeader: str = ""
    accountId: str = ""
    region: str = ""

    # --- free-form escape hatches -----------------------------------------
    extraHeaders: dict[str, str] = Field(default_factory=dict)
    extraQuery: dict[str, str] = Field(default_factory=dict)
    extraBody: dict[str, Any] = Field(default_factory=dict)

    # --- behaviour --------------------------------------------------------
    requestTimeoutSeconds: float = 0.0
    supportsJsonMode: bool = True
    supportsReasoningControl: bool = False
    enabled: bool = True

    # --- credential -------------------------------------------------------
    # Optional here, and never stored on the provider row: when present it is
    # forwarded to the same encrypted credential store every shipped provider
    # uses. Accepting it on this call exists purely so adding a provider is one
    # action rather than two - the key is saved by the same code path the
    # rotation endpoint uses, with the same encryption and the same eviction.
    apiKey: str = ""

    def definition(self) -> dict[str, Any]:
        """The payload minus the credential, for ``CustomProviderSpec``."""
        payload = self.model_dump()
        payload.pop("apiKey", None)
        return payload
