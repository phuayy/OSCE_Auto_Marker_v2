from __future__ import annotations

import pytest

from app.core.webhook_url import (
    MAX_WEBHOOK_URL_LENGTH,
    WebhookUrlError,
    validate_webhook_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://example.com/",
        "ftp://example.com/hook",
        "javascript:alert(1)",
    ],
)
def test_non_http_schemes_are_refused(url) -> None:
    """Only http(s) is a webhook. Other schemes are classic SSRF pivots."""
    with pytest.raises(WebhookUrlError, match="http"):
        validate_webhook_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8787/hook",
        "http://localhost:8787/hook",  # resolves to loopback
        "http://10.0.0.5/hook",
        "http://192.168.1.10/hook",
        "http://172.16.4.4/hook",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata endpoint
        "http://[::1]/hook",
    ],
)
def test_private_and_loopback_targets_are_refused_by_default(url) -> None:
    """The server must not be steerable at its own trust boundary.

    169.254.169.254 is the case that matters most: on a cloud host it hands out
    instance credentials to anything that can make it issue a GET.
    """
    with pytest.raises(WebhookUrlError):
        validate_webhook_url(url)


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:9000/hook", "http://localhost:9000/hook", "http://192.168.0.9/hook"],
)
def test_private_targets_allowed_when_explicitly_opted_in(url) -> None:
    """Local development needs to point a webhook at its own machine."""
    assert validate_webhook_url(url, allow_private=True) == url


def test_empty_url_is_refused() -> None:
    with pytest.raises(WebhookUrlError, match="required"):
        validate_webhook_url("")


def test_url_without_host_is_refused() -> None:
    with pytest.raises(WebhookUrlError, match="host"):
        validate_webhook_url("http:///no-host-here")


def test_overlong_url_is_refused() -> None:
    url = "https://example.com/" + ("x" * MAX_WEBHOOK_URL_LENGTH)
    with pytest.raises(WebhookUrlError, match="characters or fewer"):
        validate_webhook_url(url)


def test_unresolvable_host_is_refused() -> None:
    """A name that resolves to nothing cannot be proven safe, so it is refused."""
    with pytest.raises(WebhookUrlError, match="could not be resolved"):
        validate_webhook_url("https://this-host-does-not-exist.invalid/hook")


def test_url_is_returned_unchanged_when_allowed() -> None:
    """allow_private short-circuits DNS, so this needs no network access."""
    url = "https://hooks.example.com/services/abc?x=1"
    assert validate_webhook_url(url, allow_private=True) == url
