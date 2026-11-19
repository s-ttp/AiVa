"""Browser CORS on the gateway (local dev and CI; deployed gateways set no origins, ADR-0019).

A browser sends the AiVa credential only in X-AiVa-Authorization. A preflight asking for Authorization is
refused, so a client that regresses to it fails in local dev and CI before it deploys.
"""

from httpx import ASGITransport, AsyncClient

from aiva_gateway.main import create_app

ORIGIN = "http://localhost:5173"


def _client(services) -> AsyncClient:  # type: ignore[no-untyped-def]
    settings = services.settings.model_copy(update={"cors_origins": [ORIGIN]})
    app = create_app(settings=settings, services=services)
    app.state.services = services
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway")


async def _preflight(client: AsyncClient, headers: str, origin: str = ORIGIN):  # type: ignore[no-untyped-def]
    return await client.options(
        "/v1/catalog/scenarios",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": headers,
        },
    )


async def test_a_browser_may_send_the_credential_only_in_x_aiva_authorization(services) -> None:  # type: ignore[no-untyped-def]
    async with _client(services) as client:
        allowed = await _preflight(client, "x-aiva-authorization,content-type,idempotency-key")
        authorization = await _preflight(client, "authorization")
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == ORIGIN
    assert "access-control-allow-credentials" not in allowed.headers
    assert authorization.status_code == 400  # "Disallowed CORS headers"


async def test_only_the_configured_origin_is_allowed(services) -> None:  # type: ignore[no-untyped-def]
    async with _client(services) as client:
        other = await _preflight(client, "x-aiva-authorization", origin="https://example.com")
    assert other.status_code == 400
    assert "access-control-allow-origin" not in other.headers
