"""Router behaviour: retries, the mode ladder, fallback, and the breaker.

These are the properties an assessment depends on when a vendor misbehaves, so
they are tested against scripted fake providers rather than a live API. Sleep
and the clock are injected, so a test that exercises six retries and a cooldown
still runs instantly.
"""
from __future__ import annotations

import pytest

from app.llm.base import (
    AllTargetsFailedError,
    ChatRequest,
    ChatResponse,
    LLMConfigError,
    LLMProvider,
    LLMRequestError,
    LLMResponseError,
    LLMTransportError,
    LLMValidationError,
    ModelSpec,
    ProviderCredentials,
    ProviderDescriptor,
    RequestMode,
)
from app.llm.retry import CircuitBreaker, backoff_delay
from app.llm.router import LLMRouter
from app.llm.routing import LLMTarget, RetryPolicy, RoutingConfig


def descriptor(provider_id: str) -> ProviderDescriptor:
    return ProviderDescriptor(
        id=provider_id,
        label=provider_id.title(),
        vendor=provider_id,
        description="test provider",
        api_key_env=(f"{provider_id.upper()}_API_KEY",),
        default_base_url=f"https://{provider_id}.test/v1",
        models=(ModelSpec(id=f"{provider_id}-model", label="Test model"),),
    )


class ScriptedProvider(LLMProvider):
    """Returns each scripted outcome in turn; an Exception is raised, text is returned."""

    def __init__(self, provider_id: str, script: list[object]) -> None:
        super().__init__(ProviderCredentials(api_key="key"))
        self.descriptor = descriptor(provider_id)
        self.script = list(script)
        self.calls: list[tuple[str, str]] = []

    def complete(self, request: ChatRequest, model: str, mode: RequestMode) -> ChatResponse:
        self.calls.append((model, mode.value))
        outcome = self.script.pop(0) if self.script else "default response text"
        if isinstance(outcome, Exception):
            raise outcome
        return ChatResponse(
            content=str(outcome),
            provider_id=self.descriptor.id,
            model=model,
            mode=mode.value,
            finish_reason="stop",
        )


def build_router(providers: dict[str, LLMProvider], config: RoutingConfig) -> tuple[LLMRouter, list[float]]:
    slept: list[float] = []
    ticks = iter(range(0, 10_000))
    router = LLMRouter(
        providers,
        config,
        sleep=slept.append,
        clock=lambda: float(next(ticks)),
    )
    return router, slept


def request() -> ChatRequest:
    return ChatRequest(messages=[{"role": "user", "content": "hi"}], label="test")


FAST_RETRY = RetryPolicy(
    max_attempts_per_mode=3,
    initial_backoff_seconds=0.01,
    max_backoff_seconds=0.02,
    jitter_ratio=0.0,
)


def test_transient_failure_is_retried_within_the_same_mode() -> None:
    provider = ScriptedProvider("nvidia", [LLMTransportError("503 upstream"), "recovered"])
    config = RoutingConfig(primary=LLMTarget("nvidia", "m1"), retry=FAST_RETRY)
    router, slept = build_router({"nvidia": provider}, config)

    response = router.complete(request())

    assert response.content == "recovered"
    assert [mode for _model, mode in provider.calls] == ["structured", "structured"]
    assert len(slept) == 1


def test_request_shape_rejection_advances_the_mode_without_retrying() -> None:
    """A 400 on response_format is the exact case the ladder exists for."""
    provider = ScriptedProvider("nvidia", [LLMRequestError("400 unknown field"), "plain output"])
    config = RoutingConfig(primary=LLMTarget("nvidia", "m1"), retry=FAST_RETRY)
    router, slept = build_router({"nvidia": provider}, config)

    response = router.complete(request())

    assert response.content == "plain output"
    assert [mode for _model, mode in provider.calls] == ["structured", "json_only"]
    assert slept == []


def test_bad_credentials_skip_straight_to_the_fallback() -> None:
    primary = ScriptedProvider("openai", [LLMConfigError("401 invalid api key")])
    fallback = ScriptedProvider("deepseek", ["fallback output"])
    config = RoutingConfig(
        primary=LLMTarget("openai", "gpt"),
        fallbacks=(LLMTarget("deepseek", "deepseek-chat"),),
        retry=FAST_RETRY,
    )
    router, slept = build_router({"openai": primary, "deepseek": fallback}, config)

    response = router.complete(request())

    assert response.provider_id == "deepseek"
    # One doomed call only: no retries, no mode ladder for a rejected key.
    assert len(primary.calls) == 1
    assert slept == []


def test_fallback_runs_after_the_primary_exhausts_every_mode() -> None:
    primary = ScriptedProvider("nvidia", [LLMTransportError("boom")] * 9)
    fallback = ScriptedProvider("anthropic", ["claude output"])
    config = RoutingConfig(
        primary=LLMTarget("nvidia", "m1"),
        fallbacks=(LLMTarget("anthropic", "claude-sonnet-5"),),
        retry=FAST_RETRY,
    )
    router, _slept = build_router({"nvidia": primary, "anthropic": fallback}, config)

    response = router.complete(request())

    assert response.provider_id == "anthropic"
    assert len(primary.calls) == 9  # three modes x three attempts
    assert response.attempts[-1].ok is True


def test_caller_validation_failure_is_retried_then_falls_back() -> None:
    """A short criteria array must not be validated into a sheet of defaults."""
    primary = ScriptedProvider("nvidia", ["truncated"] * 9)
    fallback = ScriptedProvider("openai", ["complete"])
    config = RoutingConfig(
        primary=LLMTarget("nvidia", "m1"),
        fallbacks=(LLMTarget("openai", "gpt-4.1"),),
        retry=FAST_RETRY,
    )
    router, _slept = build_router({"nvidia": primary, "openai": fallback}, config)

    def validate(content: str) -> None:
        if content == "truncated":
            raise LLMValidationError("Model JSON had an incomplete criteria array")

    response = router.complete(request(), validate=validate)

    assert response.content == "complete"
    assert len(primary.calls) == 9


def test_all_targets_failing_reports_the_attempt_trail() -> None:
    primary = ScriptedProvider("nvidia", [LLMResponseError("empty content")] * 9)
    config = RoutingConfig(primary=LLMTarget("nvidia", "m1"), retry=FAST_RETRY)
    router, _slept = build_router({"nvidia": primary}, config)

    with pytest.raises(AllTargetsFailedError) as excinfo:
        router.complete(request())

    assert len(excinfo.value.attempts) == 9
    assert all(record.ok is False for record in excinfo.value.attempts)


def test_unknown_provider_in_stored_selection_is_skipped_not_fatal() -> None:
    """A settings row can outlive the provider module it names."""
    fallback = ScriptedProvider("openai", ["ok"])
    config = RoutingConfig(
        primary=LLMTarget("retired-vendor", "x"),
        fallbacks=(LLMTarget("openai", "gpt-4.1"),),
        retry=FAST_RETRY,
    )
    router, _slept = build_router({"openai": fallback}, config)

    assert [target.provider_id for target in router.resolved_targets()] == ["openai"]
    assert router.complete(request()).provider_id == "openai"


def test_no_usable_target_raises_a_configuration_error() -> None:
    config = RoutingConfig(primary=LLMTarget("retired-vendor", "x"), retry=FAST_RETRY)
    router, _slept = build_router({}, config)

    with pytest.raises(LLMConfigError):
        router.complete(request())


def test_duplicate_fallback_is_collapsed() -> None:
    """Two identical dropdown choices must not double the failure cost."""
    config = RoutingConfig(
        primary=LLMTarget("nvidia", "m1"),
        fallbacks=(LLMTarget("nvidia", "m1"),),
    )
    assert config.targets() == (LLMTarget("nvidia", "m1"),)


def test_open_circuit_skips_a_provider_while_another_target_remains() -> None:
    policy = RetryPolicy(
        max_attempts_per_mode=1,
        initial_backoff_seconds=0.0,
        max_backoff_seconds=0.0,
        jitter_ratio=0.0,
        circuit_failure_threshold=2,
        circuit_cooldown_seconds=1_000.0,
    )
    primary = ScriptedProvider("nvidia", [LLMTransportError("down")] * 10)
    fallback = ScriptedProvider("openai", ["ok", "ok"])
    config = RoutingConfig(
        primary=LLMTarget("nvidia", "m1"),
        fallbacks=(LLMTarget("openai", "gpt-4.1"),),
        retry=policy,
    )
    router, _slept = build_router({"nvidia": primary, "openai": fallback}, config)

    assert router.complete(request()).provider_id == "openai"
    calls_after_first = len(primary.calls)

    # Second call: the primary's breaker is open, so it is not attempted again.
    assert router.complete(request()).provider_id == "openai"
    assert len(primary.calls) == calls_after_first


def test_circuit_breaker_reopens_for_a_lone_target() -> None:
    """A stale breaker must never be the reason an assessment dies."""
    policy = RetryPolicy(
        max_attempts_per_mode=1,
        initial_backoff_seconds=0.0,
        max_backoff_seconds=0.0,
        jitter_ratio=0.0,
        circuit_failure_threshold=1,
        circuit_cooldown_seconds=1_000.0,
    )
    provider = ScriptedProvider("nvidia", [LLMTransportError("down")] * 3 + ["back"])
    config = RoutingConfig(primary=LLMTarget("nvidia", "m1"), retry=policy)
    router, _slept = build_router({"nvidia": provider}, config)

    with pytest.raises(AllTargetsFailedError):
        router.complete(request())
    assert router.complete(request()).content == "back"


def test_retry_after_header_wins_over_computed_backoff() -> None:
    policy = RetryPolicy(initial_backoff_seconds=1.0, max_backoff_seconds=2.0, jitter_ratio=0.0)
    assert backoff_delay(1, policy) == 1.0
    assert backoff_delay(3, policy) == 2.0
    assert backoff_delay(1, policy, retry_after_seconds=17.0) == 17.0


def test_jitter_stays_within_the_configured_band() -> None:
    policy = RetryPolicy(initial_backoff_seconds=4.0, max_backoff_seconds=4.0, jitter_ratio=0.25)
    assert backoff_delay(1, policy, rng=lambda: 0.0) == pytest.approx(3.0)
    assert backoff_delay(1, policy, rng=lambda: 1.0) == pytest.approx(5.0)


def test_breaker_half_opens_after_the_cooldown() -> None:
    now = [0.0]
    breaker = CircuitBreaker(
        RetryPolicy(circuit_failure_threshold=2, circuit_cooldown_seconds=30.0),
        clock=lambda: now[0],
    )
    breaker.record_failure("nvidia")
    breaker.record_failure("nvidia")
    assert breaker.is_open("nvidia") is True

    now[0] = 31.0
    assert breaker.is_open("nvidia") is False
