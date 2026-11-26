"""One meaning per timeout knob: per-request timeout vs. the router's total deadline."""
from __future__ import annotations

from app.core.config import Settings
from app.llm.base import LLMTransportError
from app.llm.router import LLMRouter
from app.llm.routing import LLMTarget, RetryPolicy, RoutingConfig
from app.llm.runtime import request_defaults_from_env

from tests.test_llm_deadline import Clock, TimedProvider


def test_default_deadline_is_unchanged_for_the_content_scorer() -> None:
    request = request_defaults_from_env({})
    assert request.timeout_seconds == 360
    assert request.deadline_seconds() == 900


def test_long_per_request_timeout_lifts_the_derived_deadline() -> None:
    request = request_defaults_from_env({}, timeout_seconds=5000)
    assert request.deadline_seconds() >= 5000


def test_explicit_total_is_respected() -> None:
    request = request_defaults_from_env({"LLM_TOTAL_TIMEOUT_SECONDS": "300"}, timeout_seconds=5000)
    assert request.deadline_seconds() == 300


def test_slow_communication_call_is_not_cut_at_900_seconds() -> None:
    clock = Clock()
    primary = TimedProvider("primary", clock, 1200, "valid")
    fallback = TimedProvider("fallback", clock, 1, "valid")
    router = LLMRouter(
        {"primary": primary, "fallback": fallback},
        RoutingConfig(primary=LLMTarget("primary", "m"), fallbacks=(LLMTarget("fallback", "m"),)),
        clock=clock.read, sleep=clock.sleep,
    )
    request = request_defaults_from_env({}, timeout_seconds=5000)
    assert router.complete(request).content == "valid"
    assert primary.timeouts == [5000]


def test_derived_deadline_still_bounds_retries_and_fallbacks() -> None:
    clock = Clock()
    provider = TimedProvider("p", clock, 3000, LLMTransportError("timeout"))
    router = LLMRouter(
        {"p": provider},
        RoutingConfig(primary=LLMTarget("p", "m"), retry=RetryPolicy(initial_backoff_seconds=0)),
        clock=clock.read, sleep=clock.sleep,
    )
    request = request_defaults_from_env({}, timeout_seconds=5000)
    try:
        router.complete(request)
    except Exception:
        pass
    assert clock.now <= request.deadline_seconds() + request.timeout_seconds
    assert len(provider.timeouts) < 20


def test_warning_when_total_deadline_is_below_request_timeout(monkeypatch) -> None:
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_SECONDS", "600")
    monkeypatch.setenv("LLM_TOTAL_TIMEOUT_SECONDS", "300")
    assert any("LLM_TOTAL_TIMEOUT_SECONDS" in w for w in Settings().collect_runtime_warnings())


def test_no_warning_for_consistent_timeouts(monkeypatch) -> None:
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_SECONDS", "300")
    monkeypatch.setenv("LLM_TOTAL_TIMEOUT_SECONDS", "900")
    assert not any("LLM_TOTAL_TIMEOUT_SECONDS" in w for w in Settings().collect_runtime_warnings())
