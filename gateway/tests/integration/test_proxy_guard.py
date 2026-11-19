"""Behind the platform proxy only the proxies reach the API (ADR-0007 amendment): every request without a
valid proxy proof is refused with 401 before routing, except the container's own health probes, and a
websocket handshake without it is closed with 4401. Only lifespan events pass unconditionally."""

from typing import Any

import pytest
from starlette.types import Receive, Scope, Send

from aiva_gateway.api.proxy_guard import WEBSOCKET_PROOF_REQUIRED, ProxyProofMiddleware
from tests.integration.test_public_demo import SECRET, proxied, through_proxy

GUARDED = [
    ("GET", "/v1/auth/me"),
    ("GET", "/v1/auth/sign-in-options"),
    ("POST", "/v1/auth/demo/token"),
    ("POST", "/v1/auth/public/session"),
    ("POST", "/v1/ai/chat"),
    ("GET", "/openapi.json"),
    ("GET", "/docs"),
    ("GET", "/no-such-route"),
]


@pytest.mark.parametrize(("method", "path"), GUARDED)
@pytest.mark.parametrize("proof", [None, "", SECRET[:-1], SECRET + "x", SECRET.upper()])
async def test_without_a_valid_proof_nothing_is_served(
    services, make_client, user_token, method, path, proof
) -> None:  # type: ignore[no-untyped-def]
    # Even with a valid AiVa credential: reaching the private Space with an HF token alone is not enough.
    headers = {"X-AiVa-Authorization": f"Bearer {user_token('u-tester')}"}
    if proof is not None:
        headers["X-AiVa-Proxy-Secret"] = proof
    async with make_client(svc=proxied(services), headers=headers) as client:
        response = await client.request(method, path, json={} if method == "POST" else None)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "PROXY_PROOF_REQUIRED"


async def test_the_containers_health_probes_need_no_proof(services, make_client) -> None:  # type: ignore[no-untyped-def]
    async with make_client(svc=proxied(services)) as client:
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")
    assert live.status_code == 200 and live.json() == {"status": "ok"}
    assert ready.status_code in (200, 503) and "policy_engine" in ready.json()["checks"]


async def test_a_valid_proof_is_served_as_before(services, make_client, user_token) -> None:  # type: ignore[no-untyped-def]
    async with make_client(svc=proxied(services), headers=through_proxy(user_token("u-tester"))) as client:
        me = await client.get("/v1/auth/me")
        spec = await client.get("/openapi.json")
    assert me.status_code == 200 and me.json()["user_id"] == "u-tester"
    assert spec.status_code == 200


async def test_direct_ingress_has_no_proxy_guard(make_client, user_token) -> None:  # type: ignore[no-untyped-def]
    async with make_client(user_token("u-tester")) as client:
        assert (await client.get("/v1/auth/me")).status_code == 200


# --- Websocket and lifespan scopes (the middleware as an ASGI app) -------------------------------------


async def _call(scope: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    sent: list[dict[str, Any]] = []
    reached: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        reached.append(scope["type"])

    async def receive() -> dict[str, Any]:
        return {"type": f"{scope['type']}.connect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await ProxyProofMiddleware(inner, secret=SECRET)(scope, receive, send)
    return sent, reached


def _websocket(headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
    return {"type": "websocket", "path": "/v1/stream", "headers": headers}


async def test_a_websocket_without_the_proof_is_closed_before_it_is_accepted() -> None:
    for headers in ([], [(b"x-aiva-proxy-secret", b"wrong")], [(b"authorization", b"Bearer hf_x")]):
        sent, reached = await _call(_websocket(headers))
        assert sent == [{"type": "websocket.close", "code": WEBSOCKET_PROOF_REQUIRED}] and reached == []


async def test_a_websocket_with_the_proof_and_lifespan_events_pass() -> None:
    sent, reached = await _call(_websocket([(b"x-aiva-proxy-secret", SECRET.encode())]))
    assert (sent, reached) == ([], ["websocket"])
    sent, reached = await _call({"type": "lifespan"})
    assert (sent, reached) == ([], ["lifespan"])


async def test_the_health_exemption_is_for_http_only() -> None:
    sent, reached = await _call({"type": "websocket", "path": "/health/live", "headers": []})
    assert sent[0]["type"] == "websocket.close" and reached == []
