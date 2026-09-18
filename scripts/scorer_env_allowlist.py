"""What each scorer subprocess may readmit from ``.env`` when run by hand.

Companion to ``env_loader.py``: the parent process already forwards a scorer
the exact, curated environment it needs for a real run (``app/core/
subprocess_env.py``'s allowlist plus whatever ``LLMSettingsService.
subprocess_env_for`` builds for that call). ``.env`` is loaded on top of that
only so a developer can run a script directly with no parent process at all —
and an unrestricted load would readmit everything else in ``.env``
(``AUTH_SECRET``, ``DATABASE_URL``, every provider key this deployment holds)
the moment it runs as a real subprocess, since almost none of that is already
set in the curated environment it starts with.

These frozensets are least-privilege per script: a script gets only the
names it (or the ``app.llm`` router code it imports via ``llm_bootstrap``)
actually reads, not a blanket set shared by everything.
"""

from __future__ import annotations

# Routing/catalogue: how app.llm.runtime.build_router_from_env picks a
# target and, when no OSCE_LLM_ROUTING is set (a bare by-hand run), the
# legacy NVIDIA_MODEL_NAME / NVIDIA_FALLBACK_MODELS fallback it reconstructs
# a config from.
_ROUTING = frozenset(
    {
        "OSCE_LLM_ROUTING",
        "OSCE_LLM_CUSTOM_PROVIDERS",
        "NVIDIA_MODEL_NAME",
        "NVIDIA_FALLBACK_MODELS",
    }
)

# Retry/backoff policy (app.llm.runtime, gated the same way for every provider).
_RETRY = frozenset(
    {
        "LLM_MAX_ATTEMPTS_PER_MODE",
        "LLM_INITIAL_BACKOFF_SECONDS",
        "LLM_MAX_BACKOFF_SECONDS",
        "LLM_BACKOFF_JITTER_RATIO",
        "LLM_CIRCUIT_FAILURE_THRESHOLD",
        "LLM_CIRCUIT_COOLDOWN_SECONDS",
    }
)

# Sampling, both the shared LLM_* spelling and the legacy NVIDIA_* one
# app.llm.runtime still reads.
_SAMPLING = frozenset(
    {
        "LLM_ENABLE_THINKING",
        "NVIDIA_ENABLE_THINKING",
        "LLM_REASONING_BUDGET",
        "NVIDIA_REASONING_BUDGET",
        "LLM_TEMPERATURE",
        "NVIDIA_TEMPERATURE",
        "LLM_TOP_P",
        "NVIDIA_TOP_P",
        "LLM_MAX_TOKENS",
        "NVIDIA_MAX_TOKENS",
        "LLM_REQUEST_TIMEOUT_SECONDS",
        "NVIDIA_REQUEST_TIMEOUT_SECONDS",
        "LLM_TOTAL_TIMEOUT_SECONDS",
    }
)

# Every shipped provider's key and base-URL override
# (app.llm.credentials.resolve_all reads all of them, not only the routed
# one, so a bare by-hand run needs whichever the operator is testing).
_PROVIDER_CREDENTIALS = frozenset(
    {
        "NVIDIA_API_KEY",
        "NVIDIA_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_BASE_URL",
        "OPENROUTER_API_KEY",
        "OPENROUTER_BASE_URL",
    }
)

# Shared by every script that calls into app.llm (content marking, panel
# adjudication, communication scoring, transcript preprocessing).
LLM_SCORER_ENV_ALLOWLIST: frozenset[str] = _ROUTING | _RETRY | _SAMPLING | _PROVIDER_CREDENTIALS

# nvidia_osce_communication.py's own extra knob (nvidia_osce_communication.py
# reads NVIDIA_REASONING_BUDGET / NVIDIA_REQUEST_TIMEOUT_SECONDS too, both
# already in LLM_SCORER_ENV_ALLOWLIST above).
NVIDIA_OSCE_COMMUNICATION_ENV_ALLOWLIST: frozenset[str] = LLM_SCORER_ENV_ALLOWLIST | frozenset(
    {"NVIDIA_COMMUNICATION_ENABLE_THINKING"}
)

# nemotron_transcript_preprocessor.py's own extra knobs.
NEMOTRON_PREPROCESSOR_ENV_ALLOWLIST: frozenset[str] = LLM_SCORER_ENV_ALLOWLIST | frozenset(
    {
        "NVIDIA_PREPROCESS_TEMPERATURE",
        "NVIDIA_PREPROCESS_MAX_TOKENS",
        "NVIDIA_PREPROCESS_MAX_INPUT_CHARS",
    }
)

# audio_professionalism_extractor.py has no LLM code path at all — it reads
# only FFMPEG_BIN (app.llm names above would be dead weight, and every one of
# them withheld is one less thing a crash or a stray env dump can leak).
AUDIO_EXTRACTOR_ENV_ALLOWLIST: frozenset[str] = frozenset({"FFMPEG_BIN"})
