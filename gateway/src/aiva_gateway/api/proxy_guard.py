"""Behind the platform proxy, only the proxies reach the API (ADR-0007 amendment, ADR-0019).

With `AIVA_INGRESS=platform-proxy`, a request without a valid `X-AiVa-Proxy-Secret` is refused with 401
before routing, authenticated or not, so a caller who can reach the private Space with an HF token alone
still cannot use it. A websocket handshake without it is closed with 4401; only lifespan events pass
unconditionally. The container's own probes are exempt: `/health/live` (the image's HEALTHCHECK) and
`/health/ready` (the watchdog and the smoke test). Only the proof header is read; `Authorization`, which
carries the platform's token there, is never decoded.
"""

from starlette.types import ASGIApp, Receive, Scope, Send

from aiva_gateway.api.errors import error_response
from aiva_gateway.auth.ingress import PROXY_SECRET_HEADER, proxy_proof_valid
from aiva_gateway.contracts.common import ErrorBody

UNGUARDED_PATHS = frozenset({"/health/live", "/health/ready"})
WEBSOCKET_PROOF_REQUIRED = 4401
_HEADER = PROXY_SECRET_HEADER.encode("latin-1")


class ProxyProofMiddleware:
    def __init__(self, app: ASGIApp, *, secret: str) -> None:
        self.app = app
        self.secret = secret

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Only the server's lifespan events pass unconditionally; every connection (HTTP or websocket)
        # needs the proof, apart from the container's HTTP probes.
        if scope["type"] == "lifespan" or (scope["type"] == "http" and scope["path"] in UNGUARDED_PATHS):
            await self.app(scope, receive, send)
            return
        presented = next((value for name, value in scope.get("headers", []) if name == _HEADER), None)
        if presented is not None and proxy_proof_valid(presented.decode("latin-1"), self.secret):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            # Refuse the handshake: the client sees the close before any accept (4401: proof required).
            await send({"type": "websocket.close", "code": WEBSOCKET_PROOF_REQUIRED})
            return
        if scope["type"] != "http":
            return  # an unknown connection type is never served
        response = error_response(
            401,
            ErrorBody(
                code="PROXY_PROOF_REQUIRED",
                message="This gateway is reached through the AiVa proxies only.",
                remediation="Use the AiVa Control Center, or send X-AiVa-Proxy-Secret with a direct call.",
            ),
        )
        await response(scope, receive, send)
