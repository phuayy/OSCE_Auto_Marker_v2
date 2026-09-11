"""Operator-defined scoring providers: definition, catalogue, handoff, lifecycle.

The claims worth testing here are the ones the feature rests on, not "the
endpoint returns 201":

* a definition is validated once, at one boundary, and the rules that make a
  connection work (or make it unsafe) are enforced there;
* a stored row cannot redefine a provider this build ships — that would be a way
  to repoint ``openai`` at somebody else's endpoint and hand it this
  deployment's key;
* the credential lives in the existing encrypted store, so a custom vendor
  inherits rotation rather than reimplementing it, and never comes back out of
  the API;
* what the API decided and what a scoring subprocess does are the same thing,
  because the definitions travel in the environment alongside the routing;
* an edit evicts the cached catalogue in the process that made it, so the very
  response to the edit reflects it.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import settings as settings_routes
from app.core.config import Settings
from app.database.orm import OrmDatabase
from app.llm import registry
from app.llm.catalog import (
    CUSTOM_PROVIDERS_ENV_VAR,
    builtin_catalog,
    catalog_from_env,
)
from app.llm.credentials import credential_env_for, resolve_credentials
from app.llm.custom import CustomProviderError, CustomProviderSpec, key_env_name
from app.llm.providers.custom import CustomAnthropicProvider, CustomOpenAIProvider
from app.repositories.custom_provider_repository import CustomProviderRepository
from app.services.container import create_container
from app.services.custom_provider_service import CustomProviderService


GATEWAY = {
    "id": "campus-gateway",
    "label": "Campus AI Gateway",
    "vendor": "University IT",
    "baseUrl": "https://llm.example.edu/v1",
}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_ambient_vendor_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run as if the machine exports no vendor keys.

    A developer with OPENAI_API_KEY in their shell would otherwise see providers
    report themselves configured when nothing was configured here.
    """
    for descriptor in registry.DESCRIPTORS.values():
        for name in descriptor.api_key_env:
            monkeypatch.delenv(name, raising=False)


def build_service(tmp_path: Path) -> CustomProviderService:
    database = OrmDatabase(tmp_path / "providers.sqlite3")
    asyncio.run(database.initialize())
    return CustomProviderService(CustomProviderRepository(database))


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
    # The credential store derives its master key from the auth secret, which
    # AuthService only loads during startup.
    asyncio.run(container.auth.initialize())
    asyncio.run(container.orm_database.initialize())

    app = FastAPI()
    app.state.container = container
    app.include_router(settings_routes.router, prefix="/api")
    return TestClient(app)


def provider_in(body: dict, provider_id: str) -> dict | None:
    return next((item for item in body["providers"] if item["id"] == provider_id), None)


# ---------------------------------------------------------------------------
# The definition itself
# ---------------------------------------------------------------------------


def test_a_minimal_definition_needs_only_an_id_a_label_and_an_endpoint() -> None:
    """The union is optional by default — that is the whole point of a union."""
    spec = CustomProviderSpec.from_raw(GATEWAY)

    assert spec.id == "campus-gateway"
    assert spec.api_format == "openai"
    assert spec.auth_scheme == "bearer"
    # Nothing else had to be supplied, and nothing else is invented.
    assert spec.headers() == {}
    assert spec.query() == {}


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ({**GATEWAY, "id": "Campus Gateway"}, "provider id"),
        ({**GATEWAY, "label": ""}, "name"),
        ({**GATEWAY, "baseUrl": ""}, "base URL"),
        ({**GATEWAY, "baseUrl": "file:///etc/passwd"}, "https://"),
        ({**GATEWAY, "baseUrl": "https://"}, "host name"),
        ({**GATEWAY, "apiFormat": "grpc"}, "API format"),
        ({**GATEWAY, "authScheme": "magic"}, "authentication scheme"),
        ({**GATEWAY, "authScheme": "header"}, "header name"),
        ({**GATEWAY, "authScheme": "query"}, "parameter name"),
        ({**GATEWAY, "extraHeaders": {"X-Bad": "a\r\nInjected: yes"}}, "line breaks"),
        ({**GATEWAY, "extraHeaders": {"Host": "evil.example"}}, "transport header"),
        ({**GATEWAY, "requestTimeoutSeconds": -5}, "between 0"),
    ],
)
def test_a_definition_that_could_not_work_is_refused_with_a_usable_message(
    payload: dict, fragment: str
) -> None:
    """Every rule is enforced at one boundary, so the route, the subprocess
    loader and any future importer cannot drift apart."""
    with pytest.raises(CustomProviderError) as error:
        CustomProviderSpec.from_raw(payload)
    assert fragment in str(error.value)


def test_a_file_url_is_refused_because_the_server_is_the_one_that_fetches_it() -> None:
    """An endpoint is an address this server connects to on an operator's say-so.
    Anything but http(s) would make the settings form a request-forgery tool."""
    with pytest.raises(CustomProviderError):
        CustomProviderSpec.from_raw({**GATEWAY, "baseUrl": "gopher://internal/"})


def test_deployment_identifiers_are_substituted_into_the_endpoint() -> None:
    """Azure and Cloudflare put the region or the account in the URL. Keeping
    them in their own fields means changing region is not a URL re-edit."""
    spec = CustomProviderSpec.from_raw(
        {
            **GATEWAY,
            "baseUrl": "https://{region}.api.example.com/accounts/{accountId}/v1",
            "region": "eu-west",
            "accountId": "acct-42",
        }
    )
    assert spec.resolved_base_url() == "https://eu-west.api.example.com/accounts/acct-42/v1"


def test_each_auth_scheme_puts_the_key_where_that_platform_expects_it() -> None:
    bearer = CustomProviderSpec.from_raw(GATEWAY)
    assert bearer.sends_bearer_header() is True
    assert "x-api-key" not in bearer.headers("secret")

    azure = CustomProviderSpec.from_raw(
        {
            **GATEWAY,
            "authScheme": "header",
            "authHeaderName": "api-key",
            "apiVersion": "2024-10-21",
            "apiVersionQueryParam": "api-version",
        }
    )
    assert azure.headers("secret")["api-key"] == "secret"
    assert azure.query("secret") == {"api-version": "2024-10-21"}
    assert azure.sends_bearer_header() is False

    anthropic_style = CustomProviderSpec.from_raw(
        {
            **GATEWAY,
            "authScheme": "header",
            "authHeaderName": "x-api-key",
            "apiVersion": "2023-06-01",
            "apiVersionHeader": "anthropic-version",
        }
    )
    headers = anthropic_style.headers("secret")
    assert headers == {"x-api-key": "secret", "anthropic-version": "2023-06-01"}

    google_style = CustomProviderSpec.from_raw(
        {**GATEWAY, "authScheme": "query", "authQueryParam": "key"}
    )
    assert google_style.query("secret") == {"key": "secret"}
    assert google_style.headers("secret") == {}


def test_a_prefixed_authorization_header_is_expressible() -> None:
    """Some vendors want `Authorization: Api-Key <key>`, not Bearer."""
    spec = CustomProviderSpec.from_raw(
        {
            **GATEWAY,
            "authScheme": "header",
            "authHeaderName": "Authorization",
            "authValuePrefix": "Api-Key ",
        }
    )
    assert spec.headers("secret") == {"Authorization": "Api-Key secret"}


def test_a_definition_never_carries_a_credential() -> None:
    """to_public() is serialised into an environment variable and logged, so the
    invariant is that there is nothing in it to leak."""
    spec = CustomProviderSpec.from_raw({**GATEWAY, "apiKey": "sk-should-be-ignored"})
    serialised = json.dumps(spec.to_public())

    assert "sk-should-be-ignored" not in serialised
    assert "apiKey" not in spec.to_public()


def test_the_descriptor_offers_no_model_shortlist_and_allows_any_model_id() -> None:
    """A definition describes a *platform*. The model is a separate decision with
    a separate lifetime and its own card, so the routing screen must ask for an
    id rather than offer a list this record could not have."""
    descriptor = CustomProviderSpec.from_raw(GATEWAY).to_descriptor()

    assert descriptor.models == ()
    assert descriptor.allows_custom_model is True
    assert descriptor.is_custom is True
    assert descriptor.api_key_env == (key_env_name("campus-gateway"),)


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


def test_a_custom_provider_joins_the_catalogue_alongside_the_shipped_ones() -> None:
    catalog = builtin_catalog().with_custom([CustomProviderSpec.from_raw(GATEWAY)])

    assert set(registry.provider_ids()) <= set(catalog.provider_ids())
    assert catalog.contains("campus-gateway")
    assert catalog.is_custom("campus-gateway")
    assert catalog.is_custom("openai") is False


def test_a_definition_cannot_redefine_a_provider_this_build_ships() -> None:
    """A row that could repoint 'openai' at another endpoint would be a way to
    have this deployment send its OpenAI key somewhere of the row author's
    choosing. The shipped provider wins, always."""
    hostile = CustomProviderSpec.from_raw(
        {**GATEWAY, "id": "openai", "baseUrl": "https://attacker.example/v1"}
    )
    catalog = builtin_catalog().with_custom([hostile])

    assert catalog.is_custom("openai") is False
    assert catalog.descriptor_for("openai").default_base_url == "https://api.openai.com/v1"


def test_a_disabled_definition_is_absent_from_the_catalogue_not_merely_greyed_out() -> None:
    spec = CustomProviderSpec.from_raw({**GATEWAY, "enabled": False})
    assert builtin_catalog().with_custom([spec]).contains("campus-gateway") is False


def test_the_catalogue_builds_the_adapter_that_matches_the_wire_format() -> None:
    catalog = builtin_catalog().with_custom(
        [
            CustomProviderSpec.from_raw(GATEWAY),
            CustomProviderSpec.from_raw(
                {
                    **GATEWAY,
                    "id": "claude-relay",
                    "apiFormat": "anthropic",
                    "authScheme": "header",
                    "authHeaderName": "x-api-key",
                }
            ),
        ]
    )
    providers = catalog.build_all({})

    assert isinstance(providers["campus-gateway"], CustomOpenAIProvider)
    assert isinstance(providers["claude-relay"], CustomAnthropicProvider)
    # Every provider is built even with no key, so the settings screen can say
    # *why* one is unavailable instead of omitting it.
    assert providers["campus-gateway"].availability().available is False


def test_a_custom_provider_reports_its_own_id_not_the_adapter_s() -> None:
    """The Anthropic adapter used to hardcode its module-level provider id;
    a relay must report itself so a failure trail names the right target."""
    spec = CustomProviderSpec.from_raw({**GATEWAY, "id": "claude-relay", "apiFormat": "anthropic"})
    provider = builtin_catalog().with_custom([spec]).build_all({})["claude-relay"]

    assert provider.descriptor.id == "claude-relay"
    assert "OSCE_LLM_KEY_CLAUDE_RELAY" in provider.availability().reason


# ---------------------------------------------------------------------------
# The subprocess handoff
# ---------------------------------------------------------------------------


def test_definitions_reach_a_subprocess_through_the_environment() -> None:
    """A scorer has no database. It learns what 'campus-gateway' means from the
    variable the API serialised, which is why the two can never disagree."""
    catalog = builtin_catalog().with_custom([CustomProviderSpec.from_raw(GATEWAY)])
    env = catalog.to_env()

    rebuilt = catalog_from_env(env)

    assert rebuilt.contains("campus-gateway")
    assert rebuilt.spec_for("campus-gateway").resolved_base_url() == GATEWAY["baseUrl"]
    # Credential-free, so the blob is safe to log next to the routing.
    assert "OSCE_LLM_KEY" not in env[CUSTOM_PROVIDERS_ENV_VAR] or "sk-" not in env[
        CUSTOM_PROVIDERS_ENV_VAR
    ]


def test_a_deployment_with_no_custom_providers_hands_over_the_environment_it_always_did() -> None:
    assert builtin_catalog().to_env() == {}
    assert catalog_from_env({}).provider_ids() == registry.provider_ids()


def test_a_corrupt_definitions_variable_degrades_to_the_shipped_providers() -> None:
    """Scoring must survive a bad blob; the shipped vendors are still callable."""
    catalog = catalog_from_env({CUSTOM_PROVIDERS_ENV_VAR: "{not json"})
    assert catalog.provider_ids() == registry.provider_ids()


def test_one_unusable_definition_does_not_take_out_the_others() -> None:
    payload = json.dumps(
        {
            "schema": "osce-llm-custom-providers-v1",
            "providers": [
                {"id": "broken", "label": "Broken"},  # no base URL
                CustomProviderSpec.from_raw(GATEWAY).to_public(),
            ],
        }
    )
    catalog = catalog_from_env({CUSTOM_PROVIDERS_ENV_VAR: payload})

    assert catalog.contains("campus-gateway")
    assert catalog.contains("broken") is False


def test_a_custom_provider_s_key_is_resolved_and_forwarded_like_any_other() -> None:
    """No special case: the generated variable name is on the descriptor, so the
    existing resolver finds it and the existing forwarder narrows to it."""
    catalog = builtin_catalog().with_custom([CustomProviderSpec.from_raw(GATEWAY)])
    env = {key_env_name("campus-gateway"): "sk-campus-1234"}

    resolved = resolve_credentials("campus-gateway", env, catalog=catalog)
    assert resolved.api_key == "sk-campus-1234"
    assert resolved.base_url == GATEWAY["baseUrl"]

    forwarded = credential_env_for(["campus-gateway"], env, catalog=catalog)
    assert forwarded == {key_env_name("campus-gateway"): "sk-campus-1234"}
    # A scorer that will never call OpenAI is not handed an OpenAI key.
    assert credential_env_for(["openai"], env, catalog=catalog) == {}


# ---------------------------------------------------------------------------
# Service lifecycle and caching
# ---------------------------------------------------------------------------


def test_saving_a_definition_makes_it_routable_immediately(tmp_path: Path) -> None:
    service = build_service(tmp_path)

    asyncio.run(service.save(GATEWAY, actor="admin"))
    catalog = asyncio.run(service.catalog())

    assert catalog.contains("campus-gateway")
    assert catalog.spec_for("campus-gateway").updated_by == "admin"


def test_an_edit_is_visible_to_the_very_read_that_follows_it(tmp_path: Path) -> None:
    """A local write evicts directly rather than waiting for the database's own
    announcement to come back round — otherwise the response to an edit is built
    from the catalogue the edit replaced."""
    service = build_service(tmp_path)
    asyncio.run(service.save(GATEWAY))

    asyncio.run(service.save({**GATEWAY, "baseUrl": "https://llm2.example.edu/v1"}))

    spec = asyncio.run(service.get_spec("campus-gateway"))
    assert spec.resolved_base_url() == "https://llm2.example.edu/v1"


def test_an_update_replaces_the_definition_rather_than_merging_into_it(tmp_path: Path) -> None:
    """Merging would make 'delete this header' impossible to express."""
    service = build_service(tmp_path)
    asyncio.run(service.save({**GATEWAY, "extraHeaders": {"X-Title": "OSCE"}}))

    asyncio.run(service.save(GATEWAY))

    assert asyncio.run(service.get_spec("campus-gateway")).extra_headers == {}


def test_the_service_refuses_to_shadow_a_shipped_provider(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    with pytest.raises(CustomProviderError) as error:
        asyncio.run(service.save({**GATEWAY, "id": "anthropic"}))
    assert "already ships" in str(error.value)


def test_updating_a_provider_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    with pytest.raises(CustomProviderError):
        asyncio.run(service.save(GATEWAY, provider_id="campus-gateway", allow_create=False))


def test_deleting_removes_it_from_the_catalogue(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    asyncio.run(service.save(GATEWAY))

    assert asyncio.run(service.delete("campus-gateway")) is True
    assert asyncio.run(service.catalog()).contains("campus-gateway") is False
    assert asyncio.run(service.delete("campus-gateway")) is False


def test_without_a_change_feed_nothing_is_cached(tmp_path: Path) -> None:
    """A catalogue with no way to learn about an edit would keep sending this
    deployment's key to an endpoint the operator has already moved away from."""
    service = build_service(tmp_path)
    asyncio.run(service.catalog())
    assert service.cache_stats()["enabled"] is False


# ---------------------------------------------------------------------------
# HTTP contract
# ---------------------------------------------------------------------------


def test_a_provider_added_through_the_api_appears_in_the_settings_description(
    tmp_path: Path,
) -> None:
    client = build_client(tmp_path)

    created = client.post("/api/settings/llm-providers", json=GATEWAY)
    assert created.status_code == 201

    described = created.json()
    entry = provider_in(described, "campus-gateway")
    assert entry is not None
    assert entry["isCustom"] is True
    assert entry["connection"]["baseUrl"] == GATEWAY["baseUrl"]
    # Shipped providers are still there, and still not custom.
    assert provider_in(described, "nvidia")["isCustom"] is False


def test_a_key_supplied_when_adding_a_provider_is_stored_but_never_returned(
    tmp_path: Path,
) -> None:
    client = build_client(tmp_path)

    body = client.post(
        "/api/settings/llm-providers", json={**GATEWAY, "apiKey": "sk-campus-secret-1234"}
    ).json()

    assert "sk-campus-secret-1234" not in json.dumps(body)
    credential = provider_in(body, "campus-gateway")["credential"]
    assert credential["source"] == "app"
    assert credential["configured"] is True
    # The masked tail is the only thing that comes back, exactly as for a
    # provider this build ships.
    assert credential["maskedKey"]
    assert provider_in(body, "campus-gateway")["availability"]["available"] is True


def test_the_routing_can_select_a_custom_provider(tmp_path: Path) -> None:
    """The point of the feature: a provider that did not ship can be the primary."""
    client = build_client(tmp_path)
    client.post("/api/settings/llm-providers", json={**GATEWAY, "apiKey": "sk-campus-1234"})

    saved = client.put(
        "/api/settings",
        json={
            "llmTranscriptPreprocess": False,
            "llmPrimary": {"providerId": "campus-gateway", "model": "llama-3.3-70b"},
            "llmFallbacks": [],
        },
    )

    assert saved.status_code == 200
    described = client.get("/api/settings/llm-providers").json()
    assert described["selected"]["primary"]["providerId"] == "campus-gateway"
    # It survives filtering, because a key was saved for it.
    assert described["effective"]["primary"]["providerId"] == "campus-gateway"


def test_a_provider_id_that_exists_nowhere_is_still_rejected(tmp_path: Path) -> None:
    """Relaxing the Pydantic validator moved this check; it did not remove it."""
    client = build_client(tmp_path)

    rejected = client.put(
        "/api/settings",
        json={
            "llmTranscriptPreprocess": False,
            "llmPrimary": {"providerId": "not-a-vendor", "model": "x"},
        },
    )
    assert rejected.status_code == 422


def test_an_invalid_definition_is_reported_against_the_field_that_is_wrong(
    tmp_path: Path,
) -> None:
    client = build_client(tmp_path)

    rejected = client.post("/api/settings/llm-providers", json={**GATEWAY, "baseUrl": ""})

    assert rejected.status_code == 422
    assert "base URL" in rejected.json()["detail"]


def test_the_api_refuses_to_let_a_definition_shadow_a_shipped_provider(tmp_path: Path) -> None:
    client = build_client(tmp_path)

    rejected = client.post("/api/settings/llm-providers", json={**GATEWAY, "id": "openai"})

    assert rejected.status_code == 422
    assert "already ships" in rejected.json()["detail"]


def test_updating_uses_the_id_in_the_path_not_the_one_in_the_body(tmp_path: Path) -> None:
    """Otherwise a save could rename a provider out from under the routing that
    points at it."""
    client = build_client(tmp_path)
    client.post("/api/settings/llm-providers", json=GATEWAY)

    updated = client.put(
        "/api/settings/llm-providers/campus-gateway",
        json={**GATEWAY, "id": "something-else", "label": "Renamed"},
    )

    assert updated.status_code == 200
    body = updated.json()
    assert provider_in(body, "campus-gateway")["label"] == "Renamed"
    assert provider_in(body, "something-else") is None


def test_deleting_a_provider_takes_its_stored_key_with_it(tmp_path: Path) -> None:
    """An orphaned ciphertext row is a credential nothing can use and nothing
    will ever rotate — and would re-arm the provider if the id were reused."""
    client = build_client(tmp_path)
    client.post("/api/settings/llm-providers", json={**GATEWAY, "apiKey": "sk-campus-1234"})

    removed = client.delete("/api/settings/llm-providers/campus-gateway")
    assert removed.status_code == 200
    assert provider_in(removed.json(), "campus-gateway") is None

    recreated = client.post("/api/settings/llm-providers", json=GATEWAY).json()
    assert provider_in(recreated, "campus-gateway")["credential"]["configured"] is False


def test_a_shipped_provider_cannot_be_deleted(tmp_path: Path) -> None:
    """It is a property of the release, not of the deployment."""
    client = build_client(tmp_path)

    refused = client.delete("/api/settings/llm-providers/openai")

    assert refused.status_code == 404
    assert client.get("/api/settings/llm-providers").json()
    assert provider_in(client.get("/api/settings/llm-providers").json(), "openai") is not None


def test_a_key_can_be_rotated_for_a_custom_provider_through_the_normal_endpoint(
    tmp_path: Path,
) -> None:
    """Custom providers get rotation for free because they use the same store."""
    client = build_client(tmp_path)
    client.post("/api/settings/llm-providers", json=GATEWAY)

    rotated = client.put(
        "/api/settings/llm-providers/campus-gateway/key", json={"apiKey": "sk-rotated-9876"}
    )

    assert rotated.status_code == 200
    assert "sk-rotated-9876" not in json.dumps(rotated.json())
    assert provider_in(rotated.json(), "campus-gateway")["credential"]["source"] == "app"

    cleared = client.delete("/api/settings/llm-providers/campus-gateway/key")
    assert provider_in(cleared.json(), "campus-gateway")["credential"]["configured"] is False


def test_a_connection_test_names_the_unknown_provider_rather_than_erroring(
    tmp_path: Path,
) -> None:
    """A failed probe is a result the screen renders, not an API failure."""
    client = build_client(tmp_path)

    body = client.post(
        "/api/settings/llm-providers/test", json={"providerId": "ghost", "model": "x"}
    )

    assert body.status_code == 200
    assert body.json()["ok"] is False
    assert "Unknown provider 'ghost'" in body.json()["error"]


def test_the_subprocess_environment_carries_the_definition_and_only_its_key(
    tmp_path: Path,
) -> None:
    """The end-to-end claim: what the settings screen decided is what a scorer
    runs, with no database and no restart."""
    client = build_client(tmp_path)
    client.post("/api/settings/llm-providers", json={**GATEWAY, "apiKey": "sk-campus-1234"})
    client.put(
        "/api/settings",
        json={
            "llmTranscriptPreprocess": False,
            "llmPrimary": {"providerId": "campus-gateway", "model": "llama-3.3-70b"},
            "llmFallbacks": [],
        },
    )

    container = client.app.state.container
    env = asyncio.run(container.llm_settings.subprocess_env())

    assert CUSTOM_PROVIDERS_ENV_VAR in env
    assert env[key_env_name("campus-gateway")] == "sk-campus-1234"
    routing = json.loads(env["OSCE_LLM_ROUTING"])
    assert routing["primary"] == {"providerId": "campus-gateway", "model": "llama-3.3-70b"}

    # And a process handed exactly this environment resolves the same target.
    rebuilt = catalog_from_env(env)
    assert rebuilt.contains("campus-gateway")
    assert resolve_credentials("campus-gateway", env, catalog=rebuilt).api_key == "sk-campus-1234"
