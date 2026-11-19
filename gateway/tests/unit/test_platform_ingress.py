"""Platform-proxy ingress (ADR-0019): the proxy proof, the attested client address, and what the run sees."""

from typing import Literal

import pytest

from aiva_gateway.auth.ingress import (
    PROXY_SECRET_HEADER,
    attested_client_ip,
    client_reference,
    parse_networks,
    proxy_proof_valid,
    resolve_ingress,
)
from aiva_gateway.auth.principal import RunIdentity
from aiva_gateway.auth.rbac import Permission, identity_granted
from aiva_gateway.auth.run_identity import authorize_execution_mode
from aiva_gateway.auth.tokens import pseudonymise
from aiva_gateway.core.enums import IdentityMode, SourceMode
from aiva_gateway.core.errors import PermissionDeniedError

SECRET = "s" * 40
TENANT_NETWORKS = parse_networks(["10.20.0.0/16", "192.0.2.0/24", "34.1.0.0/16"])
TRUSTED_PROXIES = parse_networks(["10.0.0.0/8"])


def _ingress(mode: str, peer: str, **headers: str):  # type: ignore[no-untyped-def]
    return resolve_ingress(
        mode=mode,
        peer=peer,
        headers={k.replace("_", "-"): v for k, v in headers.items()},
        proxy_secret=SECRET,
        trusted_proxies=TRUSTED_PROXIES,
        tenant_networks=TENANT_NETWORKS,
    )


def test_the_proxy_proof_is_an_exact_match() -> None:
    assert proxy_proof_valid(SECRET, SECRET)
    assert not proxy_proof_valid(SECRET[:-1], SECRET)
    assert not proxy_proof_valid(SECRET + "x", SECRET)
    assert not proxy_proof_valid(None, SECRET)
    assert not proxy_proof_valid(SECRET, None)


@pytest.mark.parametrize(
    ("value", "attested"),
    [
        ("8.8.8.8", "8.8.8.8"),
        (" 8.8.4.4 ", "8.8.4.4"),
        ("2606:4700:4700::1111", "2606:4700:4700::1111"),
        ("10.20.1.15", None),  # private, and a tenant campus: spoofing it attests nothing
        ("192.0.2.10", None),  # documentation range (the Test Console's simulated network)
        ("127.0.0.1", None),
        ("100.64.0.1", None),  # carrier-grade NAT
        ("34.1.2.3", None),  # public, but inside a tenant network
        ("8.8.8.8, 1.1.1.1", None),  # one address only; the proxy picks the hop
        ("not-an-ip", None),
        ("", None),
        (None, None),
    ],
)
def test_only_a_single_public_address_outside_the_tenant_is_attested(
    value: str | None, attested: str | None
) -> None:
    assert attested_client_ip(value, TENANT_NETWORKS) == attested


def test_behind_the_platform_proxy_the_peer_is_never_a_client_address() -> None:
    proxied = _ingress("platform-proxy", "10.1.2.3", x_aiva_proxy_secret=SECRET, x_aiva_client_ip="8.8.8.8")
    assert (proxied.proxied, proxied.client_ip, proxied.attested_ip) == (True, None, "8.8.8.8")
    # Without the proof nothing is trusted: neither the attestation nor X-Forwarded-For nor the peer.
    unproven = _ingress(
        "platform-proxy", "10.20.1.15", x_aiva_client_ip="8.8.8.8", x_forwarded_for="10.20.1.16"
    )
    assert (unproven.proxied, unproven.client_ip, unproven.attested_ip) == (False, None, None)


def test_direct_ingress_is_unchanged_and_ignores_the_proxy_headers() -> None:
    direct = _ingress(
        "direct",
        "10.9.9.9",
        x_forwarded_for="10.20.1.15",
        x_aiva_client_ip="8.8.8.8",
        **{PROXY_SECRET_HEADER.replace("-", "_"): SECRET},
    )
    assert (direct.proxied, direct.client_ip, direct.attested_ip) == (False, "10.20.1.15", None)
    assert _ingress("direct", "203.0.113.9", x_forwarded_for="10.20.1.15").client_ip == "203.0.113.9"


def test_a_client_reference_is_stable_and_never_the_address() -> None:
    ref = client_reference("key", "8.8.8.8")
    assert ref == client_reference("key", "8.8.8.8") != client_reference("key", "8.8.4.4")
    assert ref.startswith("cli_") and "8.8.8.8" not in ref
    assert ref != client_reference("other-key", "8.8.8.8")


def test_each_public_session_is_its_own_pseudonym() -> None:
    plain = pseudonymise("key", "u-public-visitor")
    first, second = (pseudonymise("key", "u-public-visitor", sid) for sid in ("sid-1", "sid-2"))
    assert len({plain, first, second}) == 3
    assert first == pseudonymise("key", "u-public-visitor", "sid-1")
    assert all(p.startswith("usr_") for p in (plain, first, second))


def _identity(*roles: str, kind: Literal["application", "user"] = "user") -> RunIdentity:
    return RunIdentity(
        application_id="gct-support-chatbot",
        identity_mode=IdentityMode.SIMULATED,
        source_ip="192.0.2.10",
        source_mode=SourceMode.SIMULATED,
        principal_kind=kind,
        principal_pseudonym="usr_x" if kind == "user" else None,
        roles=roles,
    )


@pytest.mark.parametrize("role", ["tester", "public_visitor", "platform_admin"])
def test_live_runs_need_chat_live(role: str) -> None:
    authorize_execution_mode(_identity(role), "live")


@pytest.mark.parametrize("roles", [("security_auditor",), ("finance_analyst",), ()])
def test_a_role_without_chat_live_may_replay_but_not_run_live(roles: tuple[str, ...]) -> None:
    authorize_execution_mode(_identity(*roles), "replay")
    with pytest.raises(PermissionDeniedError) as refused:
        authorize_execution_mode(_identity(*roles), "live")
    assert refused.value.code == "LIVE_NOT_PERMITTED"


def test_an_application_keeps_the_gateways_mode() -> None:
    authorize_execution_mode(_identity(kind="application"), "live")


def test_a_public_visitor_is_recognised_from_its_run_identity() -> None:
    assert _identity("public_visitor").is_public_visitor
    assert not _identity("tester").is_public_visitor
    assert not _identity("public_visitor", kind="application").is_public_visitor
    assert identity_granted(_identity("public_visitor"), Permission.CHAT_LIVE)
    assert not identity_granted(_identity("unknown-role"), Permission.CHAT_LIVE)
