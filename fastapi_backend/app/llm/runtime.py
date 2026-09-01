"""Building a router from the environment — the subprocess entry point.

A scoring script calls :func:`build_router_from_env` and gets whatever the
operator selected in the settings screen, because the API serialised that
selection into ``OSCE_LLM_ROUTING`` before spawning the process.

The legacy path matters just as much. Running ``python scripts/
nvidia_osce_assessor.py`` by hand — which is how the scorers are debugged — sets
no routing variable, so this module reconstructs an equivalent config from the
``NVIDIA_MODEL_NAME`` / ``NVIDIA_FALLBACK_MODELS`` variables the scripts have
always honoured. Those runs keep working unchanged.
"""
from __future__ import annotations

import os
from typing import Mapping

from app.llm import credentials as credential_resolver
from app.llm import registry
from app.llm.base import ChatRequest, ReasoningPolicy
from app.llm.router import LLMRouter
from app.llm.routing import ROUTING_ENV_VAR, LLMTarget, RetryPolicy, RoutingConfig


def _read_float(env: Mapping[str, str], name: str, fallback: float) -> float:
    try:
        return float(str(env.get(name) or "").strip())
    except (TypeError, ValueError):
        return fallback


def _read_int(env: Mapping[str, str], name: str, fallback: int) -> int:
    try:
        return int(float(str(env.get(name) or "").strip()))
    except (TypeError, ValueError):
        return fallback


def _read_bool(env: Mapping[str, str], name: str, fallback: bool) -> bool:
    raw = str(env.get(name) or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return fallback


def legacy_routing(env: Mapping[str, str]) -> RoutingConfig:
    """Reconstruct routing from the pre-router NVIDIA variables."""
    primary_model = str(env.get("NVIDIA_MODEL_NAME") or "").strip()
    fallback_models = [
        item.strip()
        for item in str(env.get("NVIDIA_FALLBACK_MODELS") or "").split(",")
        if item.strip()
    ]
    return RoutingConfig(
        primary=LLMTarget(registry.DEFAULT_PROVIDER_ID, primary_model),
        fallbacks=tuple(LLMTarget(registry.DEFAULT_PROVIDER_ID, model) for model in fallback_models),
        retry=retry_policy_from_env(env),
    )


def retry_policy_from_env(env: Mapping[str, str] | None = None) -> RetryPolicy:
    env = os.environ if env is None else env
    defaults = RetryPolicy()
    return RetryPolicy(
        max_attempts_per_mode=max(1, _read_int(env, "LLM_MAX_ATTEMPTS_PER_MODE", defaults.max_attempts_per_mode)),
        initial_backoff_seconds=_read_float(env, "LLM_INITIAL_BACKOFF_SECONDS", defaults.initial_backoff_seconds),
        max_backoff_seconds=_read_float(env, "LLM_MAX_BACKOFF_SECONDS", defaults.max_backoff_seconds),
        jitter_ratio=_read_float(env, "LLM_BACKOFF_JITTER_RATIO", defaults.jitter_ratio),
        circuit_failure_threshold=max(
            1, _read_int(env, "LLM_CIRCUIT_FAILURE_THRESHOLD", defaults.circuit_failure_threshold)
        ),
        circuit_cooldown_seconds=_read_float(
            env, "LLM_CIRCUIT_COOLDOWN_SECONDS", defaults.circuit_cooldown_seconds
        ),
    )


def routing_from_env(env: Mapping[str, str] | None = None) -> RoutingConfig:
    source = os.environ if env is None else env
    serialized = str(source.get(ROUTING_ENV_VAR) or "").strip()
    if not serialized:
        return legacy_routing(source)
    config = RoutingConfig.from_json(serialized, default_provider_id=registry.DEFAULT_PROVIDER_ID)
    # Environment overrides still win for the retry knobs: they are an
    # operational dial (tighten backoff on a flaky network) rather than a
    # per-deployment preference worth a settings row.
    if any(key.startswith("LLM_") for key in source):
        return config.with_retry(retry_policy_from_env(source))
    return config


def build_router_from_env(env: Mapping[str, str] | None = None) -> LLMRouter:
    """The router a scoring subprocess should use."""
    source = os.environ if env is None else env
    config = routing_from_env(source)
    providers = registry.build_all(credential_resolver.resolve_all(source))
    return LLMRouter(providers, config)


def request_defaults_from_env(env: Mapping[str, str] | None = None, **overrides: object) -> ChatRequest:
    """A :class:`ChatRequest` pre-filled from the sampling environment variables.

    The ``NVIDIA_*`` names are kept as the canonical spelling because they are
    what existing deployments' ``.env`` files already set; the neutral
    ``LLM_*`` names take precedence for anyone configuring this fresh.
    """
    source = os.environ if env is None else env
    thinking = _read_bool(source, "LLM_ENABLE_THINKING", _read_bool(source, "NVIDIA_ENABLE_THINKING", False))
    budget = _read_int(source, "LLM_REASONING_BUDGET", _read_int(source, "NVIDIA_REASONING_BUDGET", 16_384))
    request = ChatRequest(
        messages=[],
        temperature=_read_float(source, "LLM_TEMPERATURE", _read_float(source, "NVIDIA_TEMPERATURE", 0.2)),
        top_p=_read_float(source, "LLM_TOP_P", _read_float(source, "NVIDIA_TOP_P", 0.9)),
        max_tokens=_read_int(source, "LLM_MAX_TOKENS", _read_int(source, "NVIDIA_MAX_TOKENS", 24_576)),
        timeout_seconds=_read_float(
            source,
            "LLM_REQUEST_TIMEOUT_SECONDS",
            _read_float(source, "NVIDIA_REQUEST_TIMEOUT_SECONDS", 360.0),
        ),
        reasoning=ReasoningPolicy(
            enabled=thinking,
            effort="high" if thinking else "none",
            budget_tokens=budget,
        ),
    )
    for key, value in overrides.items():
        setattr(request, key, value)
    return request
