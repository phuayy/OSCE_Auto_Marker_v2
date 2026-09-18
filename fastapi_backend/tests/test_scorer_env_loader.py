"""scripts/env_loader.py's allowlist (A5): a scorer subprocess is started with
a curated environment (app/core/subprocess_env.py's allowlist plus whatever
the parent explicitly forwards) precisely so it does not inherit this
process's secrets. Loading .env unrestricted on top of that readmitted
almost everything else in the file, since none of it was already set in the
curated environment a real run starts with. ``allowed_keys`` is the fix;
these tests exercise it directly against the real module.
"""

from __future__ import annotations

from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _write_env(tmp_path: Path, **values: str) -> Path:
    env_path = tmp_path / ".env"
    env_path.write_text("\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8")
    return env_path


def test_allowed_keys_none_preserves_unrestricted_behaviour(tmp_path, monkeypatch):
    """The default (no allowlist) must be unchanged — debug_scripts/ and any
    other hand-run tool that never passes allowed_keys keeps working."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import env_loader

    _write_env(tmp_path, ALLOWED_ONE="visible", ALSO_UNLISTED="also-visible")
    monkeypatch.delenv("ALLOWED_ONE", raising=False)
    monkeypatch.delenv("ALSO_UNLISTED", raising=False)

    assert env_loader.load_env_file(tmp_path) is True
    import os

    assert os.environ["ALLOWED_ONE"] == "visible"
    assert os.environ["ALSO_UNLISTED"] == "also-visible"


def test_allowed_keys_admits_only_the_named_set(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import env_loader
    import os

    _write_env(
        tmp_path,
        NVIDIA_API_KEY="nvapi-secret",
        AUTH_SECRET="super-secret",
        DATABASE_URL="postgresql://user:pw@host/db",
    )
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("AUTH_SECRET", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    loaded = env_loader.load_env_file(tmp_path, allowed_keys=frozenset({"NVIDIA_API_KEY"}))

    assert loaded is True
    assert os.environ["NVIDIA_API_KEY"] == "nvapi-secret"
    assert "AUTH_SECRET" not in os.environ
    assert "DATABASE_URL" not in os.environ


def test_allowed_keys_matching_is_case_insensitive(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import env_loader
    import os

    _write_env(tmp_path, FFMPEG_BIN="/usr/bin/ffmpeg")
    monkeypatch.delenv("FFMPEG_BIN", raising=False)

    env_loader.load_env_file(tmp_path, allowed_keys=frozenset({"ffmpeg_bin"}))

    assert os.environ["FFMPEG_BIN"] == "/usr/bin/ffmpeg"


def test_an_already_set_value_is_never_overwritten_even_when_allowed(tmp_path, monkeypatch):
    """Existing behaviour, must not regress: .env only fills gaps — a value
    the parent process explicitly forwarded always wins."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import env_loader
    import os

    _write_env(tmp_path, NVIDIA_API_KEY="from-dot-env")
    monkeypatch.setenv("NVIDIA_API_KEY", "from-parent-process")

    env_loader.load_env_file(tmp_path, allowed_keys=frozenset({"NVIDIA_API_KEY"}))

    assert os.environ["NVIDIA_API_KEY"] == "from-parent-process"


def test_no_env_file_returns_false_regardless_of_allowlist(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import env_loader

    assert env_loader.load_env_file(tmp_path, allowed_keys=frozenset({"X"})) is False


# --- the per-script allowlists themselves ------------------------------------


def test_audio_extractor_allowlist_carries_no_llm_secret(monkeypatch):
    """Belt-and-suspenders companion to
    test_scoring_inputs.py::test_audio_professionalism_extractor_is_started_without_llm_credentials:
    even if that env-building test regressed, the allowlist itself must never
    grow to include a secret this script has no use for."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from scorer_env_allowlist import AUDIO_EXTRACTOR_ENV_ALLOWLIST

    assert AUDIO_EXTRACTOR_ENV_ALLOWLIST == frozenset({"FFMPEG_BIN"})
    for secret in ("OSCE_LLM_ROUTING", "NVIDIA_API_KEY", "OPENAI_API_KEY", "AUTH_SECRET", "DATABASE_URL"):
        assert secret not in AUDIO_EXTRACTOR_ENV_ALLOWLIST


def test_llm_scorer_allowlists_never_carry_deployment_secrets(monkeypatch):
    """None of the LLM-facing allowlists may ever admit the credentials that
    are not a scoring provider's own — the ones env_loader existed to leak."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from scorer_env_allowlist import (
        LLM_SCORER_ENV_ALLOWLIST,
        NEMOTRON_PREPROCESSOR_ENV_ALLOWLIST,
        NVIDIA_OSCE_COMMUNICATION_ENV_ALLOWLIST,
    )

    deployment_secrets = {
        "AUTH_SECRET",
        "DATABASE_URL",
        "APP_DATABASE_URL",
        "CREDENTIAL_ENCRYPTION_KEY",
        "SMTP_PASSWORD",
        "DEFAULT_ADMIN_PASSWORD",
        "HATCHET_CLIENT_TOKEN",
    }
    for allowlist in (
        LLM_SCORER_ENV_ALLOWLIST,
        NEMOTRON_PREPROCESSOR_ENV_ALLOWLIST,
        NVIDIA_OSCE_COMMUNICATION_ENV_ALLOWLIST,
    ):
        assert allowlist.isdisjoint(deployment_secrets)
