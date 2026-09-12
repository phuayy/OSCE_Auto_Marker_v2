"""Contracts every LLM provider implements.

The narrow surface is intentional. A provider is handed a resolved
:class:`ChatRequest`, a model id and a :class:`RequestMode`, and returns one
:class:`ChatResponse` or raises an :class:`LLMError`. Everything policy-shaped —
which model, how many retries, when to give up on a vendor — lives in the
router, so adding a provider never means re-implementing backoff.

Error classification is the other half of the contract. The router can only act
sensibly if a provider says whether a failure is worth retrying (a 503, a socket
reset, a truncated body) or is terminal for this configuration (a bad key, an
unknown model). Providers translate their SDK's exceptions into these types
rather than letting vendor exceptions leak upward.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Base class for every failure the router understands.

    ``retryable`` says whether repeating the *identical* request could plausibly
    succeed. ``retry_after_seconds`` carries a provider's own advice (the
    ``Retry-After`` header) so a rate limit is respected rather than guessed at.
    """

    retryable = False

    def __init__(
        self,
        message: str,
        *,
        provider_id: str = "",
        model: str = "",
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider_id = provider_id
        self.model = model
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class LLMConfigError(LLMError):
    """Missing key, unknown provider, unusable base URL, revoked credentials.

    Never retried and never worth trying another *mode* against: the next
    target is the only thing that can help.
    """

    retryable = False


class LLMRequestError(LLMError):
    """The provider rejected the request's shape (400/404/422).

    Not retryable as-is, but the router's mode ladder may still succeed — this
    is exactly what a vendor that does not support ``response_format`` or a
    reasoning switch returns.
    """

    retryable = False


class LLMTransportError(LLMError):
    """Timeout, connection reset, 429, or a 5xx. Worth retrying."""

    retryable = True


class LLMResponseError(LLMError):
    """The call succeeded at the HTTP layer but the body is unusable.

    Empty content, ``finish_reason=length`` with nothing to show for it, an
    error object embedded in a 200 response. Retryable: providers produce these
    intermittently, and the mode ladder often clears them.
    """

    retryable = True


class LLMValidationError(LLMResponseError):
    """The caller's own validator rejected otherwise well-formed content.

    Scoring scripts use this to reject a response whose ``criteria`` array is
    short — a provider-side structured-output bug that a retry usually clears.
    """


@dataclass(frozen=True)
class AttemptRecord:
    """One provider call, for the failure trail and the run log."""

    provider_id: str
    model: str
    mode: str
    attempt: int
    ok: bool
    error: str = ""
    elapsed_seconds: float = 0.0

    def to_public(self) -> dict[str, Any]:
        return {
            "providerId": self.provider_id,
            "model": self.model,
            "mode": self.mode,
            "attempt": self.attempt,
            "ok": self.ok,
            "error": self.error,
            "elapsedSeconds": round(self.elapsed_seconds, 3),
        }


class AllTargetsFailedError(LLMError):
    """Primary and every fallback failed. Carries the whole attempt trail.

    The trail is the point: an operator debugging a failed assessment needs to
    see that the primary was rate-limited three times and the fallback rejected
    the model id, not just the last exception.
    """

    retryable = False

    def __init__(self, message: str, attempts: tuple[AttemptRecord, ...]) -> None:
        super().__init__(message)
        self.attempts = attempts


class LLMDeadlineError(AllTargetsFailedError):
    """The completion exhausted its shared wall-clock budget."""


# ---------------------------------------------------------------------------
# Request / response
# ---------------------------------------------------------------------------


class RequestMode(str, Enum):
    """Rungs of the degradation ladder, safest first.

    Providers disagree about which optional switches they accept, and a
    rejection is indistinguishable from a bad prompt unless we retry without the
    switch. ``STRUCTURED`` asks for JSON with the provider's reasoning trace
    suppressed (hidden reasoning eats the ``max_tokens`` budget and truncates
    the visible answer); ``JSON_ONLY`` drops the reasoning controls;
    ``PLAIN`` drops everything and relies on the prompt alone.
    """

    STRUCTURED = "structured"
    JSON_ONLY = "json_only"
    PLAIN = "plain"


# The ladder the router walks for a JSON request. A non-JSON request uses PLAIN
# only — there is nothing to degrade.
JSON_MODE_LADDER: tuple[RequestMode, ...] = (
    RequestMode.STRUCTURED,
    RequestMode.JSON_ONLY,
    RequestMode.PLAIN,
)


@dataclass(frozen=True)
class ReasoningPolicy:
    """How much hidden deliberation to ask for, where the provider supports it.

    Off by default. Nemotron-class deployments bill reasoning tokens against
    ``max_tokens``, so leaving it on silently truncated scoring output.
    """

    enabled: bool = False
    effort: str = "none"
    budget_tokens: int = 16_384


@dataclass
class ChatRequest:
    """One completion, described independently of any vendor."""

    messages: list[dict[str, str]]
    temperature: float = 0.2
    top_p: float = 0.9
    max_tokens: int = 24_576
    timeout_seconds: float = 360.0
    total_timeout_seconds: float = 900.0
    # Ask for a single JSON object. Providers that expose a JSON mode get it;
    # the rest are steered by the prompt and the ladder.
    json_mode: bool = True
    reasoning: ReasoningPolicy = field(default_factory=ReasoningPolicy)
    # Content shorter than this is treated as a truncated response and retried
    # rather than parsed into an all-defaults score sheet.
    min_content_chars: int = 40
    # Free-text tag for logs, e.g. "content-scoring". Never sent to a provider.
    label: str = ""
    # Overrides the default ladder. The communication scorer needs it: with
    # reasoning enabled, asking for JSON on the first attempt made Nemotron emit
    # an empty {"schema": ""} scaffold, so that caller tries plain text first.
    mode_ladder: tuple[RequestMode, ...] | None = None

    def modes(self) -> tuple[RequestMode, ...]:
        if self.mode_ladder:
            return tuple(self.mode_ladder)
        return JSON_MODE_LADDER if self.json_mode else (RequestMode.PLAIN,)


@dataclass(frozen=True)
class ChatResponse:
    """A usable completion, plus how it was obtained.

    ``content`` is the only field the scoring scripts read; the rest exists so a
    result file can record which vendor actually produced it — the whole point
    of making the vendor swappable is knowing which one ran.
    """

    content: str
    provider_id: str
    model: str
    mode: str = RequestMode.PLAIN.value
    finish_reason: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)
    attempts: tuple[AttemptRecord, ...] = ()

    def to_public(self) -> dict[str, Any]:
        return {
            "providerId": self.provider_id,
            "model": self.model,
            "mode": self.mode,
            "finishReason": self.finish_reason,
            "usage": dict(self.usage or {}),
            "attempts": [attempt.to_public() for attempt in self.attempts],
        }


# ---------------------------------------------------------------------------
# Provider description
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSpec:
    """One model the settings screen offers for a provider.

    The list is a curated shortlist, not an exhaustive catalogue: vendors ship
    models faster than this repo is released, so ``allows_custom_model`` lets an
    operator type an id we have never heard of.
    """

    id: str
    label: str
    context_window: int = 0
    max_output_tokens: int = 0
    supports_json_mode: bool = True
    supports_reasoning_control: bool = False
    notes: str = ""

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "contextWindow": self.context_window,
            "maxOutputTokens": self.max_output_tokens,
            "supportsJsonMode": self.supports_json_mode,
            "supportsReasoningControl": self.supports_reasoning_control,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class ProviderDescriptor:
    """A vendor's identity, credentials contract and model shortlist."""

    id: str
    label: str
    vendor: str
    description: str
    # Checked in order; the first one set in the environment wins. Several
    # vendors are commonly configured under more than one conventional name.
    api_key_env: tuple[str, ...]
    default_base_url: str
    models: tuple[ModelSpec, ...]
    base_url_env: str = ""
    allows_custom_model: bool = True
    # Shown in the settings screen when the provider has no key configured.
    requirements: str = ""
    # True for a provider an operator defined at runtime rather than one this
    # build ships. The settings screen uses it to decide what may be edited:
    # a shipped provider's endpoint is a property of the release, a custom
    # one's is a property of the deployment.
    is_custom: bool = False
    # The custom provider's connection definition, echoed back so the edit form
    # can be pre-filled from the server's own copy rather than from whatever the
    # browser happened to remember. Never carries a credential — see
    # ``app/llm/custom.py``. Empty for a shipped provider.
    connection: Mapping[str, Any] = field(default_factory=dict)

    def model(self, model_id: str) -> ModelSpec | None:
        wanted = str(model_id or "").strip()
        return next((spec for spec in self.models if spec.id == wanted), None)

    def default_model_id(self) -> str:
        return self.models[0].id if self.models else ""

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "vendor": self.vendor,
            "description": self.description,
            "apiKeyEnv": list(self.api_key_env),
            "baseUrlEnv": self.base_url_env,
            "defaultBaseUrl": self.default_base_url,
            "models": [spec.to_public() for spec in self.models],
            "allowsCustomModel": self.allows_custom_model,
            "defaultModelId": self.default_model_id(),
            "requirements": self.requirements,
            "isCustom": self.is_custom,
            "connection": dict(self.connection or {}),
        }


@dataclass(frozen=True)
class ProviderCredentials:
    """Resolved secrets and endpoint for one provider on this machine."""

    api_key: str = ""
    base_url: str = ""
    extra_headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(str(self.api_key or "").strip())


@dataclass(frozen=True)
class ProviderAvailability:
    """Whether this provider could be called right now.

    Availability is a *configuration* check, not a network probe: the settings
    screen renders it for every provider on every load, and one round trip per
    vendor per page view is not a trade worth making. The explicit connection
    test is the network check.
    """

    available: bool
    reason: str = ""

    def to_public(self) -> dict[str, Any]:
        return {"available": self.available, "reason": self.reason}


# HTTP statuses where repeating the same request can plausibly succeed.
RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524})

_RETRYABLE_TEXT_TOKENS = (
    "timeout",
    "timed out",
    "temporarily unavailable",
    "rate limit",
    "rate-limit",
    "rate_limit",
    "too many requests",
    "try again",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "overloaded",
    "connection reset",
    "connection error",
    "remote disconnected",
    "read timeout",
    "provider returned error",
)

_STATUS_IN_TEXT = re.compile(r"\b(?:code|status|error)\s*[=:]?\s*(\d{3})\b")


def classify_error_text(text: str) -> bool:
    """Best-effort retryability for an error we only have prose for.

    Used where a provider embeds a failure in a 200 body, or an SDK raises a
    bare ``Exception``. Prefer the typed errors; this is the safety net.
    """

    lowered = str(text or "").lower()
    match = _STATUS_IN_TEXT.search(lowered)
    if match:
        try:
            if int(match.group(1)) in RETRYABLE_STATUS_CODES:
                return True
        except ValueError:
            pass
    return any(token in lowered for token in _RETRYABLE_TEXT_TOKENS)


def error_for_status(
    status_code: int | None,
    message: str,
    *,
    provider_id: str = "",
    model: str = "",
    retry_after_seconds: float | None = None,
) -> LLMError:
    """Map an HTTP status onto the router's error taxonomy."""

    kwargs: dict[str, Any] = {
        "provider_id": provider_id,
        "model": model,
        "status_code": status_code,
        "retry_after_seconds": retry_after_seconds,
    }
    if status_code in {401, 403}:
        return LLMConfigError(message, **kwargs)
    if status_code is not None and status_code in RETRYABLE_STATUS_CODES:
        return LLMTransportError(message, **kwargs)
    if status_code is not None and 400 <= status_code < 500:
        return LLMRequestError(message, **kwargs)
    if status_code is not None and status_code >= 500:
        return LLMTransportError(message, **kwargs)
    if classify_error_text(message):
        return LLMTransportError(message, **kwargs)
    return LLMResponseError(message, **kwargs)


def truncate_for_log(content: str, max_chars: int = 2_000) -> str:
    text = str(content or "")
    if not text:
        return "(empty)"
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}... [truncated, total {len(text)} chars]"


class LLMProvider:
    """Base class. Subclasses supply ``descriptor`` and ``complete``."""

    descriptor: ProviderDescriptor

    def __init__(self, credentials: ProviderCredentials) -> None:
        self.credentials = credentials

    @property
    def base_url(self) -> str:
        return str(self.credentials.base_url or self.descriptor.default_base_url).strip()

    def availability(self) -> ProviderAvailability:
        if not self.credentials.configured:
            names = " or ".join(self.descriptor.api_key_env)
            return ProviderAvailability(
                False,
                f"No API key configured. Set {names} in the environment or .env.",
            )
        return ProviderAvailability(True)

    def ensure_ready(self) -> None:
        """Raise a terminal error rather than making a doomed HTTP call."""
        availability = self.availability()
        if not availability.available:
            raise LLMConfigError(availability.reason, provider_id=self.descriptor.id)

    def resolve_model(self, model_id: str) -> str:
        wanted = str(model_id or "").strip()
        if wanted:
            return wanted
        default = self.descriptor.default_model_id()
        if not default:
            raise LLMConfigError(
                f"{self.descriptor.label} has no model configured and ships no default.",
                provider_id=self.descriptor.id,
            )
        return default

    def complete(self, request: ChatRequest, model: str, mode: RequestMode) -> ChatResponse:
        raise NotImplementedError

    def validate_content(
        self,
        content: str,
        finish_reason: str,
        request: ChatRequest,
        model: str,
    ) -> str:
        """Reject bodies that parse as success but carry nothing usable.

        Shared by every provider because the failure is universal: a model that
        spends its output budget on a hidden reasoning trace returns
        ``finish_reason="length"`` and an empty string, and the caller's JSON
        parser would otherwise turn that into a fully-defaulted score sheet.
        """
        text = str(content or "").strip()
        reason = str(finish_reason or "").lower()
        if reason in {"length", "max_tokens"} and len(text) < request.min_content_chars:
            raise LLMResponseError(
                "Model response was truncated (finish_reason=length) before producing usable "
                f"content. response_content={truncate_for_log(text)!r}",
                provider_id=self.descriptor.id,
                model=model,
            )
        if not text:
            raise LLMResponseError(
                f"Model response had empty content (finish_reason={reason or 'unknown'}).",
                provider_id=self.descriptor.id,
                model=model,
            )
        if len(text) < request.min_content_chars:
            raise LLMResponseError(
                f"Model response was suspiciously short ({len(text)} chars, "
                f"finish_reason={reason or 'unknown'}). "
                f"response_content={truncate_for_log(text)!r}",
                provider_id=self.descriptor.id,
                model=model,
            )
        return text
