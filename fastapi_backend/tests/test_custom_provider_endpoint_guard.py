"""A custom LLM provider's endpoint is an address this server calls on an
admin's behalf, using this deployment's own credentials — the same shape of
risk app.core.webhook_url exists for, with a different policy: a webhook
always points outside this deployment, but a custom provider routinely points
at this deployment's own network on purpose (a self-hosted vLLM box, a
departmental gateway), so private (RFC1918) space must stay reachable while
loopback, link-local (the cloud metadata address included), multicast,
reserved and unspecified addresses do not.

Sibling of test_webhook_url_guard.py; this one exercises real DNS the same
way that one does. test_custom_providers.py fakes resolution because it is
about definitions and catalogues, not about DNS.
"""

from __future__ import annotations

import pytest

from app.llm.custom import CustomProviderError, CustomProviderSpec, validate_provider_endpoint

GATEWAY = {
    "id": "campus-gateway",
    "label": "Campus AI Gateway",
    "baseUrl": "https://llm.example.edu/v1",
}


def _spec(base_url: str, **extra: str) -> CustomProviderSpec:
    return CustomProviderSpec.from_raw({**GATEWAY, "baseUrl": base_url, **extra})


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:8787/v1",
        "http://localhost:8787/v1",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata endpoint
        "http://[::1]/v1",
        "http://0.0.0.0/v1",
    ],
)
def test_loopback_and_link_local_endpoints_are_refused(base_url: str) -> None:
    """These have no legitimate inference-endpoint use — refused without an
    opt-in, unlike a webhook's private-space refusal, which is not optional."""
    with pytest.raises(CustomProviderError):
        validate_provider_endpoint(_spec(base_url))


@pytest.mark.parametrize(
    "base_url",
    ["http://10.0.0.5:8000/v1", "http://192.168.1.10:8000/v1", "http://172.16.4.4/v1"],
)
def test_private_network_endpoints_are_allowed(base_url: str) -> None:
    """The documented, intended case: a self-hosted vLLM box or departmental
    gateway on this deployment's own network. Unlike webhooks, this must not
    require an opt-in — it is the normal shape of a custom provider."""
    validate_provider_endpoint(_spec(base_url))


def test_a_real_resolving_public_host_is_allowed() -> None:
    validate_provider_endpoint(_spec("https://example.com/v1"))


def test_an_unresolvable_host_is_refused() -> None:
    with pytest.raises(CustomProviderError, match="could not be resolved"):
        validate_provider_endpoint(_spec("https://this-host-does-not-exist.invalid/v1"))


def test_the_check_runs_against_the_resolved_url_not_the_template() -> None:
    """A Cloudflare/Azure-shaped definition is not a real host until its
    placeholders are filled in. The raw template's host is the literal string
    "{region}" — not a resolvable name — so this only succeeds because the
    guard resolves ``resolved_base_url()`` (here, "example.com") rather than
    the stored template."""
    spec = _spec("https://{region}/v1", region="example.com")
    assert spec.base_url != spec.resolved_base_url()
    validate_provider_endpoint(spec)


def test_a_template_that_resolves_to_a_blocked_host_is_still_refused() -> None:
    spec = _spec("http://{region}/v1", region="127.0.0.1")
    with pytest.raises(CustomProviderError):
        validate_provider_endpoint(spec)
