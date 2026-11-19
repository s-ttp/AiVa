from aiva_gateway.auth.ingress import parse_networks, resolve_client_ip

TRUSTED = parse_networks(["10.0.0.0/8"])


def test_untrusted_peer_forwarded_for_is_ignored() -> None:
    assert resolve_client_ip("203.0.113.5", "10.20.1.15", TRUSTED) == "203.0.113.5"


def test_trusted_proxy_chain_is_walked_right_to_left() -> None:
    assert resolve_client_ip("10.0.0.2", "198.51.100.7, 10.0.0.9", TRUSTED) == "198.51.100.7"


def test_spoofed_leftmost_entry_does_not_win() -> None:
    assert resolve_client_ip("10.0.0.2", "10.20.1.15, 198.51.100.7", TRUSTED) == "198.51.100.7"


def test_malformed_forwarded_for_yields_no_ip() -> None:
    assert resolve_client_ip("10.0.0.2", "not-an-ip", TRUSTED) is None


def test_no_peer() -> None:
    assert resolve_client_ip(None, "1.2.3.4", TRUSTED) is None
