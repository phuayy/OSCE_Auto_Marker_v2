from __future__ import annotations

from urllib.parse import urlparse

from app.core.network_guard import AddressPolicy, resolve_hostname

# Schemes we are willing to POST to. Anything else (file:, gopher:, ftp:) is a
# well-known SSRF pivot and has no legitimate webhook use.
ALLOWED_SCHEMES = frozenset({"http", "https"})

MAX_WEBHOOK_URL_LENGTH = 2048

# A webhook always points at something external to this deployment — every
# non-public address class is refused. Compare app.llm.custom's policy, which
# deliberately allows private (RFC1918) space because a custom LLM provider
# routinely IS on this deployment's own network.
_ADDRESS_POLICY = AddressPolicy()


class WebhookUrlError(ValueError):
    """Raised when a webhook URL is malformed or points somewhere unsafe."""


def _is_blocked_address(address: str) -> bool:
    """True when ``address`` is a non-public IP a webhook must not reach.

    Registering ``http://127.0.0.1:8787/api/...`` or ``http://169.254.169.254/``
    would turn this feature into a server-side request forgery primitive: the
    caller supplies a URL and the *server* — inside the trust boundary, holding
    its own credentials — makes the request. Everything outside the public
    unicast range is refused unless the operator opts in.
    """
    return _ADDRESS_POLICY.is_blocked(address)


def _resolve(host: str) -> list[str]:
    """Every address ``host`` resolves to, or [] when resolution fails.

    All of them are checked, not just the first: a hostname that resolves to one
    public and one loopback address must still be refused.
    """
    return resolve_hostname(host)


def validate_webhook_url(raw_url: str, *, allow_private: bool = False) -> str:
    """Return the normalised URL, or raise :class:`WebhookUrlError`.

    ``allow_private`` re-enables loopback/private targets. It exists for local
    development (pointing a webhook at a listener on the same machine) and is
    off by default, so the safe behaviour is the one you get without reading the
    documentation.

    Note the inherent DNS-rebinding gap: the name is resolved here, and again by
    the HTTP client at send time. Closing it fully requires pinning the resolved
    address into the connection, which is out of proportion for a single-operator
    deployment; this check stops the accidental and the casual case.
    """
    url = str(raw_url or "").strip()
    if not url:
        raise WebhookUrlError("Webhook URL is required.")
    if len(url) > MAX_WEBHOOK_URL_LENGTH:
        raise WebhookUrlError(f"Webhook URL must be {MAX_WEBHOOK_URL_LENGTH} characters or fewer.")

    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise WebhookUrlError("Webhook URL must start with http:// or https://.")
    if not parsed.hostname:
        raise WebhookUrlError("Webhook URL must include a host.")

    if allow_private:
        return url

    host = parsed.hostname
    if _is_blocked_address(host):
        raise WebhookUrlError(
            "Webhook URL points at a private or loopback address. "
            "Set WEBHOOK_ALLOW_PRIVATE_URLS=true to allow this in local development."
        )

    addresses = _resolve(host)
    if not addresses:
        raise WebhookUrlError(f"Webhook URL host '{host}' could not be resolved.")
    if any(_is_blocked_address(address) for address in addresses):
        raise WebhookUrlError(
            f"Webhook URL host '{host}' resolves to a private or loopback address. "
            "Set WEBHOOK_ALLOW_PRIVATE_URLS=true to allow this in local development."
        )
    return url


__all__ = ["ALLOWED_SCHEMES", "MAX_WEBHOOK_URL_LENGTH", "WebhookUrlError", "validate_webhook_url"]
