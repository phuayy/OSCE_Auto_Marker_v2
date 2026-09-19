"""A scorer/media subprocess inherits a curated allowlist, not this process's
whole environment. See app/core/subprocess_env.py and CLAUDE.md "Subprocess
blast radius".
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from app.core.process import CommandRunner
from app.core.subprocess_env import inherited_env


def test_platform_plumbing_is_inherited(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/tester")

    env = inherited_env()

    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/tester"


def test_a_secret_is_not_inherited(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@host/db")
    monkeypatch.setenv("AUTH_SECRET", "top-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")

    env = inherited_env()

    assert "DATABASE_URL" not in env
    assert "AUTH_SECRET" not in env
    assert "OPENAI_API_KEY" not in env


def test_nvidia_api_key_is_not_inherited_by_the_nvidia_prefix(monkeypatch) -> None:
    """The one gotcha the module docstring calls out: a bare NVIDIA_ prefix
    would silently re-admit the key every scoring call site forwards on
    purpose and only to the target provider."""
    monkeypatch.setenv("NVIDIA_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("NVIDIA_VISIBLE_DEVICES", "0,1")

    env = inherited_env()

    assert "NVIDIA_API_KEY" not in env
    assert env["NVIDIA_VISIBLE_DEVICES"] == "0,1"


def test_os_account_identity_is_inherited(monkeypatch) -> None:
    """Not a credential — the OS username `getpass.getuser()` falls back to
    reading. Withheld, a dependency's import-time getpass call (NeMo's
    torch.compile-touching import chain, asking Inductor for a cache dir)
    hits the Windows-only absence of the `pwd` module instead of a clean
    username lookup."""
    monkeypatch.setenv("USERNAME", "tester")
    monkeypatch.setenv("USER", "tester")
    monkeypatch.setenv("LOGNAME", "tester")
    monkeypatch.setenv("LNAME", "tester")

    env = inherited_env()

    assert env["USERNAME"] == "tester"
    assert env["USER"] == "tester"
    assert env["LOGNAME"] == "tester"
    assert env["LNAME"] == "tester"


def test_accelerator_and_cache_prefixes_are_inherited(monkeypatch) -> None:
    monkeypatch.setenv("HF_HOME", "/cache/hf")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    monkeypatch.setenv("TORCH_HOME", "/cache/torch")
    monkeypatch.setenv("TRANSFORMERS_CACHE", "/cache/tf")

    env = inherited_env()

    assert env["HF_HOME"] == "/cache/hf"
    assert env["CUDA_VISIBLE_DEVICES"] == "0"
    assert env["TORCH_HOME"] == "/cache/torch"
    assert env["TRANSFORMERS_CACHE"] == "/cache/tf"


def test_an_unrelated_operator_variable_is_not_inherited(monkeypatch) -> None:
    monkeypatch.setenv("SOME_RANDOM_DOTENV_VALUE", "x")

    assert "SOME_RANDOM_DOTENV_VALUE" not in inherited_env()


def test_matching_is_case_insensitive_by_name(monkeypatch) -> None:
    # Python's os.environ already uppercases every key on Windows, so this
    # cannot be observed by setting a real env var there — it exercises
    # inherited_env()'s own comparison directly, for a POSIX process that
    # legitimately names one lowercase, or a mapping handed in some other way.
    import os

    monkeypatch.setattr(os, "environ", {"Path": "C:\\Windows", "some_random_var": "x"})

    env = inherited_env()

    assert env == {"Path": "C:\\Windows"}


# --- wired into CommandRunner ---------------------------------------------


def test_command_runner_does_not_leak_a_secret_env_var_to_the_child(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "should-not-appear")

    runner = CommandRunner(tmp_path)
    script = (
        "import json, os, sys; "
        "sys.stdout.write(json.dumps({'hasSecret': 'SUPER_SECRET_TOKEN' in os.environ, "
        "'hasPath': bool(os.environ.get('PATH') or os.environ.get('Path'))}))"
    )

    result = asyncio.run(runner.run(sys.executable, ["-c", script], "probe"))

    payload = json.loads(result.stdout)
    assert payload["hasSecret"] is False
    assert payload["hasPath"] is True  # the interpreter itself was still found and could still resolve tools


def test_command_runner_still_honours_the_callers_own_env(tmp_path: Path) -> None:
    """A caller's explicit env= still reaches the child — the allowlist only
    replaces blanket os.environ inheritance, not deliberate forwarding."""
    runner = CommandRunner(tmp_path)
    script = "import os, sys; sys.stdout.write(os.environ.get('OSCE_LLM_KEY_DEEPSEEK', ''))"

    result = asyncio.run(
        runner.run(sys.executable, ["-c", script], "probe", env={"OSCE_LLM_KEY_DEEPSEEK": "sk-forwarded"})
    )

    assert result.stdout == "sk-forwarded"
