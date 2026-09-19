"""Shared address-classification core for outbound-URL SSRF guards.

Two call sites decide whether *this server* should make an outbound request to
an operator-supplied URL: a webhook subscription
(:mod:`app.core.webhook_url`) and a custom LLM provider's endpoint
(:mod:`app.llm.custom`). Both need the same underlying question answered —
does this host resolve to an address outside the range the caller is willing
to reach — but they differ on which address classes count as "outside":

* A webhook always points at something external to this deployment (a
  third-party system, a script someone runs elsewhere), so every non-public
  address class is refused.
* A custom LLM provider routinely points at this deployment's own network on
  purpose — a self-hosted vLLM box, a departmental gateway — so private
  (RFC1918) space has to stay reachable, while loopback, link-local (the cloud
  metadata address included), multicast, reserved and unspecified addresses
  still have no legitimate inference-endpoint use.

This module holds the one implementation of "resolve and classify an address";
each caller supplies its own :class:`AddressPolicy` and writes its own error
type and message.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass


@dataclass(frozen=True)
class AddressPolicy:
    """Which non-public IP address classes a guard refuses to reach."""

    block_private: bool = True
    block_loopback: bool = True
    block_link_local: bool = True
    block_multicast: bool = True
    block_reserved: bool = True
    block_unspecified: bool = True

    def is_blocked(self, address: str) -> bool:
        """Whether the literal ``address`` falls in a class this policy refuses.

        A value that does not parse as an IP address (a bare hostname reaching
        this before DNS resolution) is never blocked here — that is
        :func:`resolve_hostname`'s job, not this one's.
        """
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return False
        return bool(
            (self.block_private and parsed.is_private)
            or (self.block_loopback and parsed.is_loopback)
            or (self.block_link_local and parsed.is_link_local)
            or (self.block_multicast and parsed.is_multicast)
            or (self.block_reserved and parsed.is_reserved)
            or (self.block_unspecified and parsed.is_unspecified)
        )


def resolve_hostname(host: str) -> list[str]:
    """Every address ``host`` resolves to, or ``[]`` when resolution fails.

    All of them are checked by callers, not just the first: a hostname that
    resolves to one public and one blocked address must still be refused.
    """
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
    except OSError:
        return []


__all__ = ["AddressPolicy", "resolve_hostname"]
