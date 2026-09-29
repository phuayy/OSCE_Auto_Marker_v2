"""TRUSTED_CLIENT_IP_HEADER: a stronger alternative to X-Forwarded-For hop-
counting for a proxy that overwrites one header with the real visitor address
rather than appending to a chain — Cloudflare's ``CF-Connecting-IP`` behind a
Cloudflare Tunnel (``cloudflared`` running natively, connecting to this API
over loopback) is the motivating case: the TCP peer is always ``127.0.0.1``,
so there is no X-Forwarded-For chain to count at all.

Mirrors the ``trusted_proxy_ips`` coverage in ``test_production_hardening.py``
(same ``_StubRequest`` shape) because the header is gated behind the exact
same proof-of-proxy requirement X-Forwarded-For already has — it is a case on
top of that logic, not a separate code path with separate trust rules.
"""

from __future__ import annotations

from app.api.dependencies import client_ip as _client_ip
from app.core.config import Settings


class _StubClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _StubRequest:
    def __init__(self, host: str, headers: dict[str, str] | None = None) -> None:
        self.client = _StubClient(host)
        self.headers = dict(headers or {})


# --- client_ip: the header only wins under all three conditions -------------


def test_trusted_peer_with_valid_header_is_used() -> None:
    request = _StubRequest("127.0.0.1", headers={"cf-connecting-ip": "198.51.100.7"})
    assert (
        _client_ip(
            request,
            trusted_proxy_count=0,
            trusted_proxy_ips=("127.0.0.1",),
            client_ip_header="cf-connecting-ip",
        )
        == "198.51.100.7"
    )


def test_untrusted_peer_with_header_falls_back_to_socket_host() -> None:
    """A client that reaches the API port directly can set any header it
    likes; without a trusted peer behind it, the header must be ignored
    exactly the way a forged X-Forwarded-For entry is."""
    request = _StubRequest("203.0.113.66", headers={"cf-connecting-ip": "198.51.100.7"})
    assert (
        _client_ip(
            request,
            trusted_proxy_count=0,
            trusted_proxy_ips=("127.0.0.1",),
            client_ip_header="cf-connecting-ip",
        )
        == "203.0.113.66"
    )


def test_header_configured_but_trusted_proxy_ips_empty_is_ignored() -> None:
    """Fail closed: naming a header alone proves nothing about who sent it.
    Falls through to the ordinary X-Forwarded-For/socket-address behaviour,
    unaffected by the header being configured at all."""
    request = _StubRequest(
        "127.0.0.1",
        headers={"cf-connecting-ip": "198.51.100.7", "x-forwarded-for": "10.9.9.9"},
    )
    assert (
        _client_ip(
            request,
            trusted_proxy_count=1,
            trusted_proxy_ips=(),
            client_ip_header="cf-connecting-ip",
        )
        == "10.9.9.9"
    )


def test_malformed_header_value_falls_back_to_xff_logic() -> None:
    """A header present but not a parseable IP address is ignored rather than
    trusted verbatim — the rate limiter must never key on arbitrary attacker-
    controlled text."""
    request = _StubRequest(
        "127.0.0.1",
        headers={"cf-connecting-ip": "not-an-ip", "x-forwarded-for": "198.51.100.7"},
    )
    assert (
        _client_ip(
            request,
            trusted_proxy_count=1,
            trusted_proxy_ips=("127.0.0.1",),
            client_ip_header="cf-connecting-ip",
        )
        == "198.51.100.7"
    )


def test_header_name_configured_but_absent_falls_back_to_xff_logic() -> None:
    request = _StubRequest("127.0.0.1", headers={"x-forwarded-for": "198.51.100.7"})
    assert (
        _client_ip(
            request,
            trusted_proxy_count=1,
            trusted_proxy_ips=("127.0.0.1",),
            client_ip_header="cf-connecting-ip",
        )
        == "198.51.100.7"
    )


def test_no_header_name_configured_is_the_unchanged_default() -> None:
    """The default empty string keeps every existing caller and test exactly
    as it behaved before this parameter existed."""
    request = _StubRequest("127.0.0.1", headers={"x-forwarded-for": "198.51.100.7"})
    assert (
        _client_ip(request, trusted_proxy_count=1, trusted_proxy_ips=("127.0.0.1",))
        == "198.51.100.7"
    )


def test_ipv6_loopback_peer_is_trusted() -> None:
    """cloudflared may bind a request over ::1 as readily as 127.0.0.1 —
    the .env.example entry documents both."""
    request = _StubRequest("::1", headers={"cf-connecting-ip": "2001:db8::7"})
    assert (
        _client_ip(
            request,
            trusted_proxy_count=0,
            trusted_proxy_ips=("::1",),
            client_ip_header="cf-connecting-ip",
        )
        == "2001:db8::7"
    )


# --- Settings: warning and production-fatal wiring ---------------------------


def _settings(**overrides: object) -> Settings:
    defaults = {
        "ffmpeg_bin": "ffmpeg",
        "ffprobe_bin": "ffprobe",
        "scorer_python_bin": "python",
    }
    return Settings(**{**defaults, **overrides})


def test_header_without_ips_produces_a_runtime_warning() -> None:
    settings = _settings(trusted_client_ip_header="cf-connecting-ip")
    warnings = settings.collect_runtime_warnings()
    assert any("TRUSTED_CLIENT_IP_HEADER" in warning for warning in warnings)


def test_header_with_ips_produces_no_warning_about_it() -> None:
    settings = _settings(
        trusted_client_ip_header="cf-connecting-ip",
        trusted_proxy_ips=("127.0.0.1", "::1"),
    )
    warnings = settings.collect_runtime_warnings()
    assert not any("TRUSTED_CLIENT_IP_HEADER" in warning for warning in warnings)


def test_no_header_configured_produces_no_warning_about_it() -> None:
    settings = _settings()
    warnings = settings.collect_runtime_warnings()
    assert not any("TRUSTED_CLIENT_IP_HEADER" in warning for warning in warnings)


def test_header_without_ips_is_fatal_in_production() -> None:
    settings = _settings(
        environment="production",
        protect_media_endpoints=True,
        trusted_client_ip_header="cf-connecting-ip",
    )
    errors = settings.startup_fatal_errors()
    assert any("TRUSTED_CLIENT_IP_HEADER" in error for error in errors)


def test_header_with_ips_in_production_has_no_fatal_errors_about_it() -> None:
    settings = _settings(
        environment="production",
        protect_media_endpoints=True,
        trusted_client_ip_header="cf-connecting-ip",
        trusted_proxy_ips=("127.0.0.1", "::1"),
    )
    errors = settings.startup_fatal_errors()
    assert not any("TRUSTED_CLIENT_IP_HEADER" in error for error in errors)


def test_header_without_ips_is_only_a_warning_outside_production() -> None:
    settings = _settings(environment="development", trusted_client_ip_header="cf-connecting-ip")
    assert settings.startup_fatal_errors() == []
