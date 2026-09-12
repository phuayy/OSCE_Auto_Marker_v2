"""Runs a completion against the configured targets until one works.

Three nested loops, each recovering from a different kind of failure:

1. **targets** — the operator's primary, then each fallback. Recovers from a
   vendor outage, a revoked key, a deprecated checkpoint.
2. **modes** — the request-shape ladder from :class:`RequestMode`. Recovers
   from a provider that rejects ``response_format`` or a reasoning switch,
   which is a 400 that no amount of retrying would fix.
3. **attempts** — jittered exponential backoff within one mode. Recovers from
   rate limits, 5xx, socket resets and truncated bodies.

Anything terminal short-circuits the loop it is in: a bad API key skips
straight to the next target rather than burning nine attempts proving the key
is still bad.

The router is synchronous by design. The real callers are the scoring
subprocesses, which are synchronous scripts; the API only ever uses it for a
connection test, which it runs on a thread.
"""
from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Mapping

from app.llm.base import (
    AllTargetsFailedError,
    AttemptRecord,
    ChatRequest,
    ChatResponse,
    LLMConfigError,
    LLMDeadlineError,
    LLMError,
    LLMProvider,
    LLMValidationError,
    RequestMode,
    classify_error_text,
)
from app.llm.deadline import bounded_completion
from app.llm.retry import CircuitBreaker, backoff_delay
from app.llm.routing import LLMTarget, RoutingConfig

logger = logging.getLogger(__name__)

# Called with the response text; raises to reject it. Used by the scorers to
# refuse a structurally-complete-looking answer whose criteria array is short.
Validator = Callable[[str], None]


class LLMRouter:
    def __init__(
        self,
        providers: Mapping[str, LLMProvider],
        config: RoutingConfig,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.providers = dict(providers)
        self.config = config
        self._sleep = sleep
        self._clock = clock
        self.breaker = breaker or CircuitBreaker(config.retry, clock=clock)

    # --- introspection -----------------------------------------------------

    def resolved_targets(self) -> tuple[LLMTarget, ...]:
        """Configured targets, minus any naming a provider this build dropped.

        A stored selection can outlive the provider module it names. Dropping
        the unknown entry (with a warning) beats failing the run, and if that
        empties the list the caller gets a clear configuration error rather
        than a confusing "no attempts were made".
        """
        resolved: list[LLMTarget] = []
        for target in self.config.targets():
            if target.provider_id in self.providers:
                resolved.append(target)
            else:
                logger.warning(
                    "Configured LLM provider '%s' is not available in this build; skipping it.",
                    target.provider_id,
                )
        return tuple(resolved)

    def describe_targets(self) -> list[dict[str, Any]]:
        return [target.to_public() for target in self.resolved_targets()]

    # --- execution ---------------------------------------------------------

    def complete(self, request: ChatRequest, *, validate: Validator | None = None) -> ChatResponse:
        if any(
            not math.isfinite(value) or value <= 0
            for value in (request.timeout_seconds, request.total_timeout_seconds)
        ):
            raise LLMConfigError("LLM timeouts must be finite positive numbers.")
        deadline = self._clock() + request.total_timeout_seconds
        targets = self.resolved_targets()
        if not targets:
            raise LLMConfigError(
                "No usable LLM target is configured. Choose a primary model in Settings, "
                "or set NVIDIA_API_KEY for the built-in default."
            )

        attempts: list[AttemptRecord] = []
        # Whether any target was skipped purely because its breaker was open.
        # If every target is skipped we retry the primary rather than failing:
        # a stale breaker must never be the reason an assessment dies.
        skipped_by_breaker: list[LLMTarget] = []

        for target in targets:
            if len(targets) > 1 and self.breaker.is_open(target.provider_id):
                logger.warning(
                    "Skipping LLM target %s: provider circuit is open after repeated failures.",
                    target.key,
                )
                skipped_by_breaker.append(target)
                continue
            response = self._run_target(target, request, validate, attempts, deadline)
            if response is not None:
                return response

        for target in skipped_by_breaker:
            if attempts and any(record.ok for record in attempts):
                break
            logger.warning("Every LLM target was circuit-broken; retrying %s anyway.", target.key)
            response = self._run_target(target, request, validate, attempts, deadline)
            if response is not None:
                return response

        self._remaining(deadline, attempts)
        summary = "; ".join(
            f"{record.provider_id}:{record.model} [{record.mode} #{record.attempt}] {record.error}"
            for record in attempts[-4:]
        )
        raise AllTargetsFailedError(
            f"All {len(targets)} configured LLM target(s) failed after {len(attempts)} attempt(s). "
            f"Last failures: {summary or 'none recorded'}",
            tuple(attempts),
        )

    # --- one target --------------------------------------------------------

    def _run_target(
        self,
        target: LLMTarget,
        request: ChatRequest,
        validate: Validator | None,
        attempts: list[AttemptRecord],
        deadline: float,
    ) -> ChatResponse | None:
        provider = self.providers[target.provider_id]
        model = target.model or provider.descriptor.default_model_id()

        for mode in request.modes():
            for attempt_index in range(1, self.config.retry.max_attempts_per_mode + 1):
                remaining = self._remaining(deadline, attempts)
                started = self._clock()
                bounded_request = replace(request, timeout_seconds=min(request.timeout_seconds, remaining))

                def invoke(
                    attempt_request: ChatRequest = bounded_request,
                    attempt_mode: RequestMode = mode,
                ) -> ChatResponse:
                    response = provider.complete(attempt_request, model, attempt_mode)
                    if validate is not None:
                        validate(response.content)
                    return response

                try:
                    response = bounded_completion(invoke, bounded_request.timeout_seconds)
                    self._remaining(deadline, attempts)
                except Exception as error:
                    elapsed = self._clock() - started
                    normalized = self._normalize(error, target.provider_id, model)
                    attempts.append(
                        AttemptRecord(
                            provider_id=target.provider_id,
                            model=model,
                            mode=mode.value,
                            attempt=attempt_index,
                            ok=False,
                            error=f"{type(normalized).__name__}: {normalized}",
                            elapsed_seconds=elapsed,
                        )
                    )
                    self.breaker.record_failure(target.provider_id)
                    logger.warning(
                        "LLM attempt failed label=%s provider=%s model=%s mode=%s attempt=%d/%d error=%s",
                        request.label or "unlabelled",
                        target.provider_id,
                        model,
                        mode.value,
                        attempt_index,
                        self.config.retry.max_attempts_per_mode,
                        normalized,
                    )
                    remaining = self._remaining(deadline, attempts)

                    if isinstance(normalized, LLMConfigError):
                        # Bad key or unusable endpoint: no mode and no retry
                        # fixes it. Move to the next target immediately.
                        return None
                    if not normalized.retryable:
                        # Wrong request shape for this vendor — the next mode is
                        # the remedy, not another identical attempt.
                        break
                    if attempt_index >= self.config.retry.max_attempts_per_mode:
                        break
                    delay = backoff_delay(
                        attempt_index,
                        self.config.retry,
                        retry_after_seconds=normalized.retry_after_seconds,
                    )
                    if delay >= remaining:
                        raise LLMDeadlineError(
                            "LLM total deadline cannot accommodate the required retry delay.",
                            tuple(attempts),
                        ) from error
                    self._sleep(delay)
                    continue

                elapsed = self._clock() - started
                attempts.append(
                    AttemptRecord(
                        provider_id=target.provider_id,
                        model=model,
                        mode=mode.value,
                        attempt=attempt_index,
                        ok=True,
                        elapsed_seconds=elapsed,
                    )
                )
                self.breaker.record_success(target.provider_id)
                if len(attempts) > 1:
                    logger.info(
                        "LLM call succeeded on %s (%s) after %d earlier attempt(s).",
                        target.key,
                        mode.value,
                        len(attempts) - 1,
                    )
                return ChatResponse(
                    content=response.content,
                    provider_id=response.provider_id,
                    model=response.model,
                    mode=response.mode,
                    finish_reason=response.finish_reason,
                    usage=response.usage,
                    attempts=tuple(attempts),
                )
        return None

    def _remaining(self, deadline: float, attempts: list[AttemptRecord]) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise LLMDeadlineError("LLM total deadline exhausted.", tuple(attempts))
        return remaining

    @staticmethod
    def _normalize(error: Exception, provider_id: str, model: str) -> LLMError:
        """Ensure every failure carries a retryability verdict.

        A validator raising a plain ``RuntimeError`` (which is what the scoring
        scripts have always raised) is treated as a retryable response problem,
        matching the behaviour those scripts relied on.
        """
        if isinstance(error, LLMError):
            return error
        message = f"{type(error).__name__}: {error}"
        if classify_error_text(message):
            from app.llm.base import LLMTransportError

            return LLMTransportError(message, provider_id=provider_id, model=model)
        return LLMValidationError(message, provider_id=provider_id, model=model)
