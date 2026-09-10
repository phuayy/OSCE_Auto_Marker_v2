"""Operator-managed provider API keys: storage, precedence and disclosure.

The tests that matter here are not "the endpoint returns 200" but the security
properties the feature rests on:

* a key put in through the API never comes back out of it;
* what lands in the database is not the key;
* a key saved in the app beats a stale one in the environment, because that is
  what makes rotation from the UI real;
* a rotated *master* key produces "re-enter this", not a decrypted mess;
* a provider that quotes the rejected credential in its error does not thereby
  publish it to the browser and the log.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.api.routes import settings as settings_routes
from app.core.config import Settings
from app.core.secret_box import SecretBox, derive_master_key, mask_secret, redact_secrets
from app.database.migration_runner import to_sync_url
from app.database.orm import OrmDatabase
from app.repositories.provider_credential_repository import ProviderCredentialRepository
from app.services.container import create_container
from app.services.llm_settings_service import LLMSettingsService
from app.services.provider_credential_service import CredentialError, ProviderCredentialService


LIVE_KEY = "sk-live-abcdefghijklmnop1234"


@pytest.fixture(autouse=True)
def _no_ambient_vendor_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run against a machine with no vendor keys exported.

    These tests assert on what is *configured*, and a developer who happens to
    have OPENAI_API_KEY in their shell would otherwise see a provider report
    itself available with nothing saved.
    """
    from app.llm import registry

    for descriptor in registry.DESCRIPTORS.values():
        for name in descriptor.api_key_env:
            monkeypatch.delenv(name, raising=False)


def build_store(tmp_path: Path, *, env_key: str = "", secret: str = "deployment-auth-secret") -> ProviderCredentialService:
    database = OrmDatabase(tmp_path / "credentials.sqlite3")
    asyncio.run(database.initialize())
    return ProviderCredentialService(
        ProviderCredentialRepository(database),
        master_key_source=lambda: secret,
        env_key=env_key,
    )


def build_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        app_database_url="",
        database_url="",
    )
    container = create_container(settings)
    asyncio.run(container.artifacts.ensure_storage_layout())
    asyncio.run(container.auth.initialize())
    asyncio.run(container.orm_database.initialize())

    app = FastAPI()
    app.state.container = container
    app.include_router(settings_routes.router, prefix="/api")
    return TestClient(app)


# --- crypto ----------------------------------------------------------------


def test_sealed_secret_round_trips_and_is_bound_to_its_provider() -> None:
    box = SecretBox(derive_master_key(auth_secret="a-deployment-secret"))
    sealed = box.seal(LIVE_KEY, aad="openai")

    assert box.open(sealed, aad="openai") == LIVE_KEY
    # Moving one provider's ciphertext onto another provider's row must fail,
    # or a database writer could make the server send OpenAI's key to NVIDIA.
    with pytest.raises(Exception):
        box.open(sealed, aad="nvidia")


def test_a_different_master_key_cannot_open_an_existing_secret() -> None:
    sealed = SecretBox(derive_master_key(auth_secret="first")).seal(LIVE_KEY, aad="openai")
    with pytest.raises(Exception):
        SecretBox(derive_master_key(auth_secret="second")).open(sealed, aad="openai")


def test_derivation_is_deterministic_but_not_the_auth_secret_itself() -> None:
    first = derive_master_key(auth_secret="stable")
    assert first == derive_master_key(auth_secret="stable")
    # The signing secret must not be reusable as the encryption key: leaking one
    # should not hand over the other.
    assert first != b"stable".ljust(32, b"\0")


def test_redaction_removes_a_key_a_provider_echoed_back() -> None:
    message = f"401 Unauthorized: key {LIVE_KEY} is not valid"
    redacted = redact_secrets(message, (LIVE_KEY,))
    assert LIVE_KEY not in redacted
    assert mask_secret(LIVE_KEY) in redacted


# --- storage ---------------------------------------------------------------


def test_stored_key_is_not_written_to_the_database_in_the_clear(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    asyncio.run(store.set_key("openai", LIVE_KEY, actor="admin"))

    engine = create_engine(to_sync_url(OrmDatabase._normalize_url(tmp_path / "credentials.sqlite3")), future=True)
    try:
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT * FROM provider_credentials")).mappings().all()
    finally:
        engine.dispose()

    assert len(rows) == 1
    dumped = " ".join(str(value) for value in rows[0].values())
    assert LIVE_KEY not in dumped
    assert rows[0]["last4"] == LIVE_KEY[-4:]
    assert asyncio.run(store.api_keys()) == {"openai": LIVE_KEY}


def test_rotation_replaces_the_previous_key_and_clears_its_verdict(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    asyncio.run(store.set_key("openai", LIVE_KEY))
    asyncio.run(store.record_test("openai", ok=True))
    assert asyncio.run(store.statuses())["openai"]["lastTestOk"] is True

    asyncio.run(store.set_key("openai", "sk-rotated-9999zzzz"))

    assert asyncio.run(store.api_keys()) == {"openai": "sk-rotated-9999zzzz"}
    status = asyncio.run(store.statuses())["openai"]
    # A green tick carried over from the previous key would vouch for a
    # credential nobody has tested.
    assert status["lastTestOk"] is None
    assert status["last4"] == "zzzz"


def test_a_row_sealed_under_a_lost_master_key_is_reported_not_returned(tmp_path: Path) -> None:
    asyncio.run(build_store(tmp_path, secret="original").set_key("openai", LIVE_KEY))

    rotated = build_store(tmp_path, secret="a-new-secret")

    assert asyncio.run(rotated.api_keys()) == {}
    assert asyncio.run(rotated.statuses())["openai"]["readable"] is False


def test_masked_previews_and_rejected_inputs(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    asyncio.run(store.set_key("openai", LIVE_KEY))

    masked = asyncio.run(store.statuses())["openai"]["maskedKey"]
    assert masked.endswith(LIVE_KEY[-4:])
    assert LIVE_KEY[:-4] not in masked

    for rejected in ("", "   ", "short", "••••••", "has space inside"):
        with pytest.raises(CredentialError):
            ProviderCredentialService.normalize_key(rejected)


# --- precedence ------------------------------------------------------------


def test_a_saved_key_overrides_the_environment_for_the_same_provider(tmp_path: Path) -> None:
    """Rotation from the UI has to win, or it is not rotation."""
    store = build_store(tmp_path)
    asyncio.run(store.set_key("nvidia", LIVE_KEY))

    from app.repositories.app_settings_repository import AppSettingsRepository

    service = LLMSettingsService(
        AppSettingsRepository(OrmDatabase(tmp_path / "settings.sqlite3")),
        key_overrides=lambda: {"nvidia": "stale-key-from-the-environment"},
        credential_store=store,
    )

    credentials = asyncio.run(service.credentials())
    assert credentials["nvidia"].api_key == LIVE_KEY
    assert asyncio.run(service.credential_sources())["nvidia"] == "app"


def test_removing_a_saved_key_hands_the_provider_back_to_the_environment(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    asyncio.run(store.set_key("nvidia", LIVE_KEY))
    asyncio.run(store.clear_key("nvidia"))

    from app.repositories.app_settings_repository import AppSettingsRepository

    service = LLMSettingsService(
        AppSettingsRepository(OrmDatabase(tmp_path / "settings.sqlite3")),
        key_overrides=lambda: {"nvidia": "environment-key-value"},
        credential_store=store,
    )

    assert asyncio.run(service.credentials())["nvidia"].api_key == "environment-key-value"
    assert asyncio.run(service.credential_sources())["nvidia"] == "environment"


def test_subprocess_env_carries_the_saved_key_for_routed_providers_only(tmp_path: Path) -> None:
    from app.repositories.app_settings_repository import AppSettingsRepository

    store = build_store(tmp_path)
    asyncio.run(store.set_key("deepseek", LIVE_KEY))
    asyncio.run(store.set_key("openai", "sk-unrouted-provider-key"))

    app_settings = AppSettingsRepository(OrmDatabase(tmp_path / "settings.sqlite3"))
    asyncio.run(
        app_settings.set_values(
            {"llmPrimary": {"providerId": "deepseek", "model": "deepseek-chat"}, "llmFallbacks": []}
        )
    )
    service = LLMSettingsService(app_settings, credential_store=store)

    env = asyncio.run(service.subprocess_env())

    assert env["DEEPSEEK_API_KEY"] == LIVE_KEY
    # A scorer that will never call OpenAI has no reason to hold its key: a
    # crash dump or a leaked log should expose as little as possible.
    assert "OPENAI_API_KEY" not in env


# --- API surface -----------------------------------------------------------


def test_saving_a_key_never_returns_it_and_marks_the_provider_available(tmp_path: Path) -> None:
    client = build_client(tmp_path)

    response = client.put("/api/settings/llm-providers/openai/key", json={"apiKey": LIVE_KEY})
    assert response.status_code == 200
    body = response.json()
    assert LIVE_KEY not in response.text

    openai = next(provider for provider in body["providers"] if provider["id"] == "openai")
    assert openai["credential"]["source"] == "app"
    assert openai["credential"]["maskedKey"].endswith(LIVE_KEY[-4:])
    assert openai["availability"]["available"] is True
    # There is no read endpoint at all, so the settings dump cannot carry one.
    assert LIVE_KEY not in client.get("/api/settings").text
    assert LIVE_KEY not in client.get("/api/settings/llm-providers").text


def test_a_rejected_key_is_a_400_with_a_usable_message(tmp_path: Path) -> None:
    client = build_client(tmp_path)

    assert client.put("/api/settings/llm-providers/openai/key", json={"apiKey": "  "}).status_code == 422
    short = client.put("/api/settings/llm-providers/openai/key", json={"apiKey": "sk-1"})
    assert short.status_code == 400
    assert "truncated" in short.json()["detail"]


def test_an_unknown_provider_is_a_404_rather_than_a_silent_write(tmp_path: Path) -> None:
    client = build_client(tmp_path)
    response = client.put("/api/settings/llm-providers/not-a-vendor/key", json={"apiKey": LIVE_KEY})
    assert response.status_code == 404


def test_deleting_a_key_returns_the_provider_to_unconfigured(tmp_path: Path) -> None:
    client = build_client(tmp_path)
    client.put("/api/settings/llm-providers/openai/key", json={"apiKey": LIVE_KEY})

    body = client.delete("/api/settings/llm-providers/openai/key").json()

    openai = next(provider for provider in body["providers"] if provider["id"] == "openai")
    assert openai["credential"]["source"] == "none"
    assert openai["availability"]["available"] is False
