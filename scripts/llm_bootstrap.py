"""Gives the scoring subprocesses access to the shared LLM router.

The scorers are standalone scripts launched by the API, so they do not import
the backend package the way a module inside ``fastapi_backend`` would. Rather
than maintaining a second, drifting copy of provider handling and retry policy
here, this shim puts ``fastapi_backend`` on ``sys.path`` and re-exports the one
implementation. The scripts and the API therefore always agree about which
model runs, how many times it is retried, and what counts as a retryable
failure.

The import is intentionally not optional. A scorer that silently fell back to a
hard-coded vendor would produce marks against a model the operator did not
choose, which is worse than failing loudly.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "fastapi_backend"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.env import read_bool_env, read_float_env, read_int_env  # noqa: E402
from app.core.scorer_utils import (  # noqa: E402
    clip_text,
    enforce_expected_criteria_array,
    extract_json_from_text,
    extract_primary_json_dict_from_model_output,
    format_response_content_for_log,
    format_timestamp_seconds,
    normalize_timestamp,
    read_generic_text,
    read_json_transcript_text,
    safe_extract_payload,
)
from app.core.utils import write_text_atomic  # noqa: E402
from app.llm.base import (  # noqa: E402  (path must be set first)
    AllTargetsFailedError,
    ChatRequest,
    ChatResponse,
    LLMError,
    LLMValidationError,
    ReasoningPolicy,
    RequestMode,
)
from app.llm.router import LLMRouter  # noqa: E402
from app.llm.runtime import (  # noqa: E402
    build_router_from_env,
    request_defaults_from_env,
)

__all__ = [
    "AllTargetsFailedError",
    "ChatRequest",
    "ChatResponse",
    "LLMError",
    "LLMRouter",
    "LLMValidationError",
    "ReasoningPolicy",
    "RequestMode",
    "build_chat_request",
    "build_router_from_env",
    "describe_routing",
    "request_defaults_from_env",
    "validator_from",
    "create_completion",
    "read_bool_env",
    "read_float_env",
    "read_int_env",
    "write_text_atomic",
    "clip_text",
    "enforce_expected_criteria_array",
    "extract_json_from_text",
    "extract_primary_json_dict_from_model_output",
    "format_response_content_for_log",
    "format_timestamp_seconds",
    "normalize_timestamp",
    "read_generic_text",
    "read_json_transcript_text",
    "safe_extract_payload",
]


def build_chat_request(
    messages: list[dict[str, str]],
    *,
    label: str,
    min_content_chars: int = 40,
    json_mode: bool = True,
    max_tokens: int | None = None,
    timeout_seconds: float | None = None,
    total_timeout_seconds: float | None = None,
    temperature: float | None = None,
    reasoning: ReasoningPolicy | None = None,
    mode_ladder: tuple[RequestMode, ...] | None = None,
) -> ChatRequest:
    """A request pre-filled from the sampling environment variables.

    Sampling knobs stay in the environment rather than in the settings database:
    they are a deployment-tuning concern (a smaller GPU box wants a lower token
    ceiling), not something an examiner should be changing between students.
    """
    request = request_defaults_from_env()
    request.messages = messages
    request.label = label
    request.json_mode = json_mode
    request.min_content_chars = min_content_chars
    if max_tokens is not None:
        request.max_tokens = max_tokens
    if timeout_seconds is not None:
        request.timeout_seconds = timeout_seconds
    if total_timeout_seconds is not None:
        request.total_timeout_seconds = total_timeout_seconds
    if temperature is not None:
        request.temperature = temperature
    if reasoning is not None:
        request.reasoning = reasoning
    if mode_ladder is not None:
        request.mode_ladder = mode_ladder
    return request


def describe_routing(router: LLMRouter) -> str:
    """One-line summary for the run log, e.g. ``nvidia:model -> openai:gpt-4.1``."""
    targets = router.resolved_targets()
    if not targets:
        return "(no LLM target configured)"
    return " -> ".join(target.key for target in targets)


def create_completion(
    router: LLMRouter,
    messages: list[dict[str, str]],
    *,
    label: str,
    expected_items: int | None,
    criteria_validator: Callable[[str, int | None], None],
    min_content_chars: int = 40,
    timeout_seconds: float | None = None,
    reasoning: ReasoningPolicy | None = None,
    mode_ladder: tuple[RequestMode, ...] | None = None,
) -> ChatResponse:
    request = build_chat_request(
        messages, label=label, min_content_chars=min_content_chars,
        timeout_seconds=timeout_seconds, reasoning=reasoning, mode_ladder=mode_ladder,
    )

    def check(content: str) -> None:
        criteria_validator(content, expected_items)

    return router.complete(request, validate=validator_from(check))


def validator_from(check: Callable[[str], object] | None) -> Callable[[str], None] | None:
    """Adapt a script's content check into the router's validator contract.

    Scripts raise plain ``RuntimeError`` for a response that parsed but is
    unusable; re-raising it as an ``LLMValidationError`` tells the router this
    is a retryable content problem rather than a bug in the caller.
    """
    if check is None:
        return None

    def validate(content: str) -> None:
        try:
            check(content)
        except LLMError:
            raise
        except Exception as error:
            raise LLMValidationError(str(error)) from error

    return validate
