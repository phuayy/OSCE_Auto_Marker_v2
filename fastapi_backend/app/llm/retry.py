"""Backoff arithmetic and the per-provider circuit breaker.

Kept apart from the router so both are testable without a provider: backoff is
a pure function of the attempt number, and the breaker is a small state machine
driven by an injectable clock.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Callable

from app.llm.routing import RetryPolicy


def backoff_delay(
    attempt: int,
    policy: RetryPolicy,
    *,
    retry_after_seconds: float | None = None,
    rng: Callable[[], float] = random.random,
) -> float:
    """Seconds to wait before ``attempt`` + 1.

    A provider's own ``Retry-After`` wins outright — guessing shorter than the
    window a vendor just told us about is how a rate limit turns into a ban.
    Otherwise: exponential growth, capped, with symmetric jitter so two branches
    of the same run (content and communication score in parallel) do not
    synchronise their retries into the same instant.
    """
    if retry_after_seconds is not None and retry_after_seconds > 0:
        return min(float(retry_after_seconds), max(policy.max_backoff_seconds, float(retry_after_seconds)))
    base = policy.initial_backoff_seconds * (2 ** max(0, attempt - 1))
    capped = min(policy.max_backoff_seconds, base)
    if policy.jitter_ratio <= 0:
        return capped
    spread = capped * policy.jitter_ratio
    return max(0.0, capped - spread + (rng() * 2 * spread))


@dataclass
class _BreakerState:
    consecutive_failures: int = 0
    opened_at: float = 0.0


@dataclass
class CircuitBreaker:
    """Skips a provider that has failed repeatedly, for a cooldown.

    Scoped to one process and one router instance, which is the right lifetime:
    a scoring subprocess makes a handful of calls and exits, and the state that
    matters is "this vendor has failed every time so far in *this* run, stop
    spending its timeout budget".

    The router never lets the breaker cause a total outage — if every target is
    open it retries the primary anyway. A stale open circuit must not turn a
    recovered vendor into a failed assessment.
    """

    policy: RetryPolicy
    clock: Callable[[], float] = time.monotonic
    _states: dict[str, _BreakerState] = field(default_factory=dict)

    def _state(self, provider_id: str) -> _BreakerState:
        return self._states.setdefault(provider_id, _BreakerState())

    def is_open(self, provider_id: str) -> bool:
        state = self._state(provider_id)
        if state.consecutive_failures < self.policy.circuit_failure_threshold:
            return False
        if self.clock() - state.opened_at >= self.policy.circuit_cooldown_seconds:
            # Cooldown elapsed: half-open. One more call decides its fate.
            state.consecutive_failures = self.policy.circuit_failure_threshold - 1
            return False
        return True

    def record_success(self, provider_id: str) -> None:
        self._states[provider_id] = _BreakerState()

    def record_failure(self, provider_id: str) -> None:
        state = self._state(provider_id)
        state.consecutive_failures += 1
        if state.consecutive_failures >= self.policy.circuit_failure_threshold:
            state.opened_at = self.clock()

    def snapshot(self) -> dict[str, int]:
        return {provider_id: state.consecutive_failures for provider_id, state in self._states.items()}
