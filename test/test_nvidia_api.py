#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI


ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT_DIR / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from env_loader import load_env_file  # noqa: E402


DEFAULT_NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_NVIDIA_MODEL = "nvidia/nemotron-3-super-120b-a12b"
SUCCESS_TOKEN = "NVIDIA_API_OK"


@dataclass(frozen=True)
class NvidiaSmokeConfig:
    api_key: str
    base_url: str
    model: str
    timeout_seconds: float


def _read_float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def load_nvidia_smoke_config() -> NvidiaSmokeConfig:
    """Load NVIDIA API settings without printing or persisting the API key."""
    load_env_file(ROOT_DIR)

    api_key = os.getenv("NVIDIA_API_KEY", "").strip()
    if not api_key or api_key == "<NVIDIA_API_KEY>":
        raise RuntimeError(
            "NVIDIA_API_KEY is missing. Set it in OSCE-AI-FYP/.env or export it "
            "before running this smoke test."
        )

    return NvidiaSmokeConfig(
        api_key=api_key,
        base_url=os.getenv("NVIDIA_BASE_URL", DEFAULT_NVIDIA_BASE_URL).strip()
        or DEFAULT_NVIDIA_BASE_URL,
        model=os.getenv("NVIDIA_MODEL_NAME", DEFAULT_NVIDIA_MODEL).strip()
        or DEFAULT_NVIDIA_MODEL,
        timeout_seconds=_read_float_env("NVIDIA_SMOKE_TEST_TIMEOUT_SECONDS", 60.0),
    )


def call_nvidia_model(config: NvidiaSmokeConfig) -> str:
    client = OpenAI(base_url=config.base_url, api_key=config.api_key)

    response = client.chat.completions.create(
        model=config.model,
        messages=[
            {
                "role": "system",
                "content": "You are a connectivity smoke test. Reply briefly.",
            },
            {
                "role": "user",
                "content": f"Reply with this exact token only: {SUCCESS_TOKEN}",
            },
        ],
        temperature=0,
        top_p=1,
        max_tokens=32,
        timeout=config.timeout_seconds,
        extra_body={
            "reasoning": {"enabled": False},
            "reasoning_effort": "none",
            "chat_template_kwargs": {"enable_thinking": False},
        },
    )

    if not response.choices:
        raise RuntimeError("NVIDIA API returned no choices.")

    content = response.choices[0].message.content
    if not content or not content.strip():
        raise RuntimeError("NVIDIA API returned an empty message.")

    return content.strip()


def test_nvidia_api_smoke() -> None:
    """Pytest smoke test. Opt in because it performs a live external API call."""
    import pytest

    if os.getenv("RUN_NVIDIA_API_SMOKE_TEST", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }:
        pytest.skip("Set RUN_NVIDIA_API_SMOKE_TEST=1 to run the live NVIDIA API smoke test.")

    config = load_nvidia_smoke_config()
    content = call_nvidia_model(config)
    assert content


def main() -> int:
    config = load_nvidia_smoke_config()
    content = call_nvidia_model(config)

    print("NVIDIA API call succeeded.")
    print(f"Base URL: {config.base_url}")
    print(f"Model: {config.model}")
    print(f"Response: {content}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
