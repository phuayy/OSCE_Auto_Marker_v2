"""What to call, in what order, and how hard to try.

This module is the contract between the three processes that need to agree
about routing: the API (which stores the operator's choice), the scoring
subprocesses (which make the calls) and the Hatchet worker (which spawns them).
A :class:`RoutingConfig` serialises to a single JSON string that travels to a
subprocess in one environment variable, so a scorer can never run against a
different primary than the settings screen shows.

Secrets deliberately do not live here. The config names providers and models;
API keys are passed as their own conventional environment variables, so a
routing blob can be logged without leaking a key.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from app.llm.base import LLMConfigError

# Environment variable carrying the serialised config into a subprocess.
ROUTING_ENV_VAR = "OSCE_LLM_ROUTING"

SCHEMA = "osce-llm-routing-v1"


@dataclass(frozen=True)
class LLMTarget:
    """One provider/model pair the router may call."""

    provider_id: str
    model: str = ""

    @property
    def key(self) -> str:
        return f"{self.provider_id}:{self.model}"

    def to_public(self) -> dict[str, str]:
        return {"providerId": self.provider_id, "model": self.model}

    @classmethod
    def from_raw(cls, raw: Any) -> "LLMTarget | None":
        """Parse a target from stored settings or a request body.

        Returns ``None`` for anything empty — "no fallback configured" is a
        legitimate state and must not raise.
        """
        if not isinstance(raw, Mapping):
            return None
        provider_id = str(raw.get("providerId") or raw.get("provider_id") or "").strip()
        model = str(raw.get("model") or raw.get("modelId") or "").strip()
        if not provider_id:
            return None
        return cls(provider_id=provider_id, model=model)


@dataclass(frozen=True)
class RetryPolicy:
    """How a single target is retried before the router moves on.

    The defaults reproduce the behaviour the NVIDIA scorer shipped with —
    three attempts per request shape, 1.5s doubling to a 8s ceiling — plus two
    additions: jitter, so parallel content and communication branches do not
    retry in lockstep into the same rate-limit window, and a circuit breaker,
    so a vendor that is comprehensively down costs one target's attempts rather
    than every call in the run.
    """

    max_attempts_per_mode: int = 3
    initial_backoff_seconds: float = 1.5
    max_backoff_seconds: float = 8.0
    # Fraction of the computed delay that is randomised, in both directions.
    jitter_ratio: float = 0.25
    # Consecutive failures against one provider before it is skipped.
    circuit_failure_threshold: int = 6
    circuit_cooldown_seconds: float = 60.0

    def to_public(self) -> dict[str, Any]:
        return {
            "maxAttemptsPerMode": self.max_attempts_per_mode,
            "initialBackoffSeconds": self.initial_backoff_seconds,
            "maxBackoffSeconds": self.max_backoff_seconds,
            "jitterRatio": self.jitter_ratio,
            "circuitFailureThreshold": self.circuit_failure_threshold,
            "circuitCooldownSeconds": self.circuit_cooldown_seconds,
        }

    @classmethod
    def from_raw(cls, raw: Any) -> "RetryPolicy":
        if not isinstance(raw, Mapping):
            return cls()
        defaults = cls()

        def number(key: str, camel: str, fallback: float) -> float:
            value = raw.get(camel, raw.get(key))
            try:
                return float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return fallback

        return cls(
            max_attempts_per_mode=max(1, int(number("max_attempts_per_mode", "maxAttemptsPerMode", defaults.max_attempts_per_mode))),
            initial_backoff_seconds=max(0.0, number("initial_backoff_seconds", "initialBackoffSeconds", defaults.initial_backoff_seconds)),
            max_backoff_seconds=max(0.0, number("max_backoff_seconds", "maxBackoffSeconds", defaults.max_backoff_seconds)),
            jitter_ratio=min(1.0, max(0.0, number("jitter_ratio", "jitterRatio", defaults.jitter_ratio))),
            circuit_failure_threshold=max(1, int(number("circuit_failure_threshold", "circuitFailureThreshold", defaults.circuit_failure_threshold))),
            circuit_cooldown_seconds=max(0.0, number("circuit_cooldown_seconds", "circuitCooldownSeconds", defaults.circuit_cooldown_seconds)),
        )


@dataclass(frozen=True)
class RoutingConfig:
    """The primary target, its fallbacks, and the retry policy for all of them."""

    primary: LLMTarget
    fallbacks: tuple[LLMTarget, ...] = ()
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    def targets(self) -> tuple[LLMTarget, ...]:
        """Primary first, then fallbacks, with duplicates removed.

        De-duplication is not cosmetic: a fallback identical to the primary
        would double every failure's wall-clock cost while adding no chance of
        success, and an operator can easily pick the same pair in both
        dropdowns.
        """
        ordered: list[LLMTarget] = []
        seen: set[str] = set()
        for target in (self.primary, *self.fallbacks):
            if not target or not target.provider_id or target.key in seen:
                continue
            seen.add(target.key)
            ordered.append(target)
        return tuple(ordered)

    def with_retry(self, retry: RetryPolicy) -> "RoutingConfig":
        return replace(self, retry=retry)

    def to_public(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "primary": self.primary.to_public(),
            "fallbacks": [target.to_public() for target in self.fallbacks],
            "retry": self.retry.to_public(),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_public(), separators=(",", ":"))

    @classmethod
    def from_raw(cls, raw: Any, *, default_provider_id: str) -> "RoutingConfig":
        """Build a config from stored settings, tolerating partial data.

        Stored settings outlive the release that wrote them. A missing primary
        falls back to this build's default provider rather than failing a run:
        a settings row that predates the LLM router must not make the marker
        unusable.
        """
        payload = raw if isinstance(raw, Mapping) else {}
        primary = LLMTarget.from_raw(payload.get("primary")) or LLMTarget(default_provider_id, "")
        fallbacks_raw = payload.get("fallbacks")
        fallbacks: list[LLMTarget] = []
        if isinstance(fallbacks_raw, (list, tuple)):
            for item in fallbacks_raw:
                target = LLMTarget.from_raw(item)
                if target is not None:
                    fallbacks.append(target)
        return cls(primary=primary, fallbacks=tuple(fallbacks), retry=RetryPolicy.from_raw(payload.get("retry")))

    @classmethod
    def from_json(cls, text: str, *, default_provider_id: str) -> "RoutingConfig":
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError) as error:
            raise LLMConfigError(f"{ROUTING_ENV_VAR} is not valid JSON: {error}") from error
        return cls.from_raw(parsed, default_provider_id=default_provider_id)

    def to_env(self) -> dict[str, str]:
        return {ROUTING_ENV_VAR: self.to_json()}
