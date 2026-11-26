"""A custom provider's extra headers/query/body are second credentials.

The OSCE_LLM_CUSTOM_PROVIDERS blob is documented as credential-free and safe to
log, so the extras travel in a per-provider variable (like the key), and only
for providers the routing actually names.
"""
from __future__ import annotations

import json

from app.llm.catalog import CUSTOM_PROVIDERS_ENV_VAR, builtin_catalog, catalog_from_env
from app.llm.custom import CustomProviderSpec, extras_env_name, key_env_name
from app.llm.routing import LLMTarget, RoutingConfig
from app.services.llm_settings_service import LLMSettingsService, ResolvedProviders


def _spec(provider_id: str, host: str = "a", **extra) -> CustomProviderSpec:
    return CustomProviderSpec.from_raw(
        {"id": provider_id, "label": provider_id, "baseUrl": f"https://{host}.example.com/v1", **extra}
    )


GATEWAY_EXTRAS = {
    "extraHeaders": {"X-Gateway-Token": "gw-secret-123"},
    "extraQuery": {"sig": "query-secret-456"},
    "extraBody": {"tenant_token": "body-secret-789"},
}


def _env_for(catalog, *provider_ids: str) -> dict[str, str]:
    config = RoutingConfig(primary=LLMTarget(provider_ids[0], "m"))
    resolved = ResolvedProviders(
        stored_keys={}, statuses={}, overrides={}, credentials={}, providers={}, sources={}, catalog=catalog
    )
    return LLMSettingsService.subprocess_env_for(config, resolved)


def test_the_blob_carries_no_connection_extras_and_is_safe_to_log() -> None:
    catalog = builtin_catalog().with_custom([_spec("gw", **GATEWAY_EXTRAS)])
    env = _env_for(catalog, "gw")
    blob = env[CUSTOM_PROVIDERS_ENV_VAR]
    for secret in ("gw-secret-123", "query-secret-456", "body-secret-789", "X-Gateway-Token"):
        assert secret not in blob


def test_a_routed_provider_still_sends_its_extras_after_the_handoff() -> None:
    catalog = builtin_catalog().with_custom([_spec("gw", **GATEWAY_EXTRAS)])
    rebuilt = catalog_from_env(_env_for(catalog, "gw"))
    spec = rebuilt.spec_for("gw")
    assert spec.extra_headers == {"X-Gateway-Token": "gw-secret-123"}
    assert spec.extra_query == {"sig": "query-secret-456"}
    assert spec.extra_body == {"tenant_token": "body-secret-789"}
    assert spec.headers("k")["X-Gateway-Token"] == "gw-secret-123"
    assert spec.query("k")["sig"] == "query-secret-456"


def test_an_unrouted_custom_provider_leaves_nothing_in_the_environment() -> None:
    catalog = builtin_catalog().with_custom([_spec("gw", **GATEWAY_EXTRAS), _spec("other", "b", **GATEWAY_EXTRAS)])
    env = _env_for(catalog, "gw")
    assert extras_env_name("other") not in env
    assert "other" not in env[CUSTOM_PROVIDERS_ENV_VAR]
    assert not catalog_from_env(env).contains("other")


def test_routing_only_shipped_providers_forwards_no_custom_definitions() -> None:
    catalog = builtin_catalog().with_custom([_spec("gw", **GATEWAY_EXTRAS)])
    env = _env_for(catalog, "openai")
    assert CUSTOM_PROVIDERS_ENV_VAR not in env
    assert extras_env_name("gw") not in env


def test_a_provider_without_extras_adds_no_extras_variable() -> None:
    catalog = builtin_catalog().with_custom([_spec("plain")])
    env = _env_for(catalog, "plain")
    assert extras_env_name("plain") not in env
    assert catalog_from_env(env).contains("plain")


def test_extras_variable_follows_the_key_variables_collision_rules() -> None:
    # Same slug, so the catalogue's existing collision rule (lowest id wins)
    # keeps two providers from ever sharing the extras variable either.
    assert extras_env_name("clinic-a") == extras_env_name("clinic_a")
    assert extras_env_name("clinic-a") != key_env_name("clinic-a")
    catalog = builtin_catalog().with_custom(
        [_spec("clinic_a", "b", extraHeaders={"X-T": "other"}), _spec("clinic-a", "a", extraHeaders={"X-T": "mine"})]
    )
    env = _env_for(catalog, "clinic-a")
    assert json.loads(env[extras_env_name("clinic-a")])["extraHeaders"] == {"X-T": "mine"}


def test_a_garbled_extras_variable_is_ignored_not_fatal() -> None:
    catalog = builtin_catalog().with_custom([_spec("gw", **GATEWAY_EXTRAS)])
    env = _env_for(catalog, "gw")
    env[extras_env_name("gw")] = "{not json"
    rebuilt = catalog_from_env(env)
    assert rebuilt.contains("gw")
    assert rebuilt.spec_for("gw").extra_headers == {}
