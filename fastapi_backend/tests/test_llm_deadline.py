from dataclasses import dataclass
from threading import Event
from time import monotonic

import pytest
from app.llm.base import (
    ChatRequest,
    ChatResponse,
    LLMConfigError,
    LLMDeadlineError,
    LLMTransportError,
    RequestMode,
)
from app.llm.deadline import bounded_completion
from app.llm.router import LLMRouter
from app.llm.routing import LLMTarget, RetryPolicy, RoutingConfig

from tests.test_llm_router import ScriptedProvider


@dataclass
class Clock:
    now: float = 0.0

    def read(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class TimedProvider(ScriptedProvider):
    def __init__(self, name: str, clock: Clock, elapsed: float, outcome: object) -> None:
        super().__init__(name, [outcome] * 20)
        self.clock = clock
        self.elapsed = elapsed
        self.timeouts: list[float] = []

    def complete(self, request: ChatRequest, model: str, mode: RequestMode) -> ChatResponse:
        self.timeouts.append(request.timeout_seconds)
        self.clock.sleep(self.elapsed)
        return super().complete(request, model, mode)


def test_deadline_shared_by_targets_and_request_is_not_mutated() -> None:
    clock = Clock()
    primary = TimedProvider("primary", clock, 6, LLMConfigError("bad key"))
    fallback = TimedProvider("fallback", clock, 1, "valid")
    router = LLMRouter(
        {"primary": primary, "fallback": fallback},
        RoutingConfig(primary=LLMTarget("primary", "m"), fallbacks=(LLMTarget("fallback", "m"),)),
        clock=clock.read, sleep=clock.sleep,
    )
    request = ChatRequest(messages=[], timeout_seconds=8, total_timeout_seconds=10)
    assert router.complete(request).content == "valid"
    assert primary.timeouts == [8]
    assert fallback.timeouts == [4]
    assert request.timeout_seconds == 8


def test_exhaustion_stops_retries_and_mode_ladder() -> None:
    clock = Clock()
    provider = TimedProvider("p", clock, 6, LLMTransportError("timeout"))
    router = LLMRouter(
        {"p": provider},
        RoutingConfig(primary=LLMTarget("p", "m"), retry=RetryPolicy(initial_backoff_seconds=0)),
        clock=clock.read, sleep=clock.sleep,
    )
    with pytest.raises(LLMDeadlineError) as caught:
        router.complete(ChatRequest(messages=[], total_timeout_seconds=10))
    assert len(caught.value.attempts) == 2
    assert provider.calls == [("m", "structured"), ("m", "structured")]
    assert provider.timeouts == [10, 4]


def test_retry_after_cannot_exceed_total_budget() -> None:
    clock = Clock()
    provider = TimedProvider("p", clock, 1, LLMTransportError("rate limit", retry_after_seconds=60))
    router = LLMRouter(
        {"p": provider}, RoutingConfig(primary=LLMTarget("p", "m")),
        clock=clock.read, sleep=clock.sleep,
    )
    with pytest.raises(LLMDeadlineError, match="retry delay"):
        router.complete(ChatRequest(messages=[], total_timeout_seconds=10))
    assert len(provider.calls) == 1
    assert clock.now == 1


def test_late_success_is_rejected() -> None:
    clock = Clock()
    provider = TimedProvider("p", clock, 11, "late")
    router = LLMRouter(
        {"p": provider}, RoutingConfig(primary=LLMTarget("p", "m")), clock=clock.read,
    )
    with pytest.raises(LLMDeadlineError) as caught:
        router.complete(ChatRequest(messages=[], total_timeout_seconds=10))
    assert len(caught.value.attempts) == 1
    assert not caught.value.attempts[0].ok


def test_non_cooperative_transport_cannot_hold_caller() -> None:
    release = Event()
    finished = Event()

    def blocking() -> ChatResponse:
        try:
            release.wait(timeout=5)
            return ChatResponse(content="late", provider_id="p", model="m")
        finally:
            finished.set()

    started = monotonic()
    try:
        with pytest.raises(LLMTransportError, match="wall-clock"):
            bounded_completion(blocking, 0.03)
        assert monotonic() - started < 1
    finally:
        release.set()
        assert finished.wait(timeout=1)


@pytest.mark.parametrize("budget", [0, -1, float("nan"), float("inf")])
def test_invalid_budget_fails_before_transport(budget: float) -> None:
    provider = ScriptedProvider("p", [])
    router = LLMRouter({"p": provider}, RoutingConfig(primary=LLMTarget("p", "m")))
    with pytest.raises(LLMConfigError, match="finite positive"):
        router.complete(ChatRequest(messages=[], total_timeout_seconds=budget))
    assert not provider.calls
