"""Trusted ingress (LLD §5, ADR-0007): never trust a client-supplied X-Forwarded-For from an untrusted peer.
Behind a platform proxy (ADR-0019) only a request carrying the proxy-identity secret is trusted at all."""

import hashlib
import hmac
import ipaddress
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_networks(cidrs: Sequence[str]) -> list[Network]:
    return [ipaddress.ip_network(cidr, strict=False) for cidr in cidrs]


def _is_trusted(address: str, trusted: Sequence[Network]) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in network for network in trusted)


def resolve_client_ip(peer: str | None, forwarded_for: str | None, trusted: Sequence[Network]) -> str | None:
    """Return the client IP: the TCP peer, unless it is a trusted proxy, in which case walk
    X-Forwarded-For right to left past trusted proxies and take the first untrusted hop."""
    if peer is None:
        return None
    if not forwarded_for or not _is_trusted(peer, trusted):
        return peer
    hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
    for hop in reversed(hops):
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            return None
        if not _is_trusted(hop, trusted):
            return hop
    return hops[0] if hops else peer


def is_valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


# --- Platform proxy (ADR-0019) ---------------------------------------------------------------------------
# Header names as the proxies send them. `Authorization` is the platform's token and is never read.
CREDENTIAL_HEADER = "x-aiva-authorization"
PROXY_SECRET_HEADER = "x-aiva-proxy-secret"  # noqa: S105 - a header name
CLIENT_IP_HEADER = "x-aiva-client-ip"


@dataclass(frozen=True)
class Ingress:
    """How a request reached the gateway.

    `client_ip` is the address the policy sees (the run's observed source). `attested_ip` is the visitor
    address a proxy vouched for; it only keys rate limits through `client_reference`, and is never recorded.
    """

    proxied: bool
    client_ip: str | None
    attested_ip: str | None


def proxy_proof_valid(presented: str | None, secret: str | None) -> bool:
    """Constant-time comparison of the proxy-identity header (digests, so lengths never leak)."""
    if not presented or not secret:
        return False
    return hmac.compare_digest(
        hashlib.sha256(presented.encode()).digest(), hashlib.sha256(secret.encode()).digest()
    )


def attested_client_ip(value: str | None, tenant_networks: Sequence[Network]) -> str | None:
    """The proxy's client address, if it is one public address outside every tenant network. A private,
    reserved or documentation address, or one inside a tenant network, cannot be a real visitor's: it means
    a misconfigured or spoofed hop, so it attests nothing."""
    if not value:
        return None
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if not ip.is_global or any(ip in network for network in tenant_networks):
        return None
    return str(ip)


def resolve_ingress(
    *,
    mode: str,
    peer: str | None,
    headers: Mapping[str, str],
    proxy_secret: str | None,
    trusted_proxies: Sequence[Network],
    tenant_networks: Sequence[Network],
) -> Ingress:
    if mode != "platform-proxy":
        return Ingress(
            proxied=False,
            client_ip=resolve_client_ip(peer, headers.get("x-forwarded-for"), trusted_proxies),
            attested_ip=None,
        )
    # Behind the platform's edge the socket peer is the platform, never a client, and X-Forwarded-For is
    # whatever the edge and the visitor wrote. Only the proxy's attestation counts, and it is not a source
    # address for policy: a visitor is on the internet, which no trusted network contains (null source).
    proxied = proxy_proof_valid(headers.get(PROXY_SECRET_HEADER), proxy_secret)
    attested = attested_client_ip(headers.get(CLIENT_IP_HEADER), tenant_networks) if proxied else None
    return Ingress(proxied=proxied, client_ip=None, attested_ip=attested)


def client_reference(pseudonym_key: str, address: str) -> str:
    """Pseudonymous per-client key for the sign-in and public-session rate limits (ADR-0019). The raw
    address is never stored; only this HMAC is, as a short-lived Redis key."""
    digest = hmac.new(pseudonym_key.encode(), f"client:{address}".encode(), hashlib.sha256).hexdigest()
    return "cli_" + digest[:32]
