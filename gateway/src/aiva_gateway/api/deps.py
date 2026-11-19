from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from aiva_gateway.auth.authenticate import authenticate
from aiva_gateway.auth.ingress import CREDENTIAL_HEADER, Ingress, parse_networks, resolve_ingress
from aiva_gateway.auth.principal import Caller
from aiva_gateway.auth.run_identity import authorize_run_caller
from aiva_gateway.services import Services


class _PlatformAwareBearer(HTTPBearer):
    """`Authorization: Bearer`, except behind a platform proxy, where that header is the platform's own
    token (the HF token): it is then never read, so it is never parsed, logged, stored or echoed."""

    async def __call__(self, request: Request) -> HTTPAuthorizationCredentials | None:
        if get_services(request).settings.ingress == "platform-proxy":
            return None
        return await super().__call__(request)


_bearer = _PlatformAwareBearer(
    auto_error=False,
    scheme_name="HTTPBearer",
    description=(
        "Application credential (aiva_…) or user JWT. Behind the platform proxy (ADR-0019) send it as "
        "`X-AiVa-Authorization: Bearer …` instead; `Authorization` there is the platform's token."
    ),
)


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


async def get_session(services: ServicesDep) -> AsyncIterator[AsyncSession]:
    async with services.sessionmaker() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def request_ingress(request: Request, services: Services) -> Ingress:
    """How this request arrived (ADR-0007, ADR-0019); resolved once per request."""
    cached: Ingress | None = getattr(request.state, "aiva_ingress", None)
    if cached is not None:
        return cached
    settings = services.settings
    ingress = resolve_ingress(
        mode=settings.ingress,
        peer=request.client.host if request.client else None,
        headers=request.headers,
        proxy_secret=settings.proxy_secret.get_secret_value() if settings.proxy_secret else None,
        trusted_proxies=services.trusted_proxies,
        tenant_networks=parse_networks([n.cidr for n in services.catalog.tenant.trusted_networks]),
    )
    request.state.aiva_ingress = ingress
    return ingress


async def get_caller(
    request: Request,
    services: ServicesDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Caller:
    # X-AiVa-Authorization wins; `credentials` is always None behind a platform proxy.
    header = request.headers.get(CREDENTIAL_HEADER)
    if header is None and credentials is not None:
        header = f"Bearer {credentials.credentials}"
    # A session of its own, returned to the pool before the handler runs. The request-scoped SessionDep
    # stays checked out until the response is sent, so authenticating through it made every request that
    # opens its own sessions hold two pooled connections; at pool size + overflow concurrent requests each
    # held one and waited for a second until the pool timeout (500s under load).
    async with services.sessionmaker() as session:
        return await authenticate(header, session, services.settings)


CallerDep = Annotated[Caller, Depends(get_caller)]


async def get_run_caller(caller: CallerDep) -> Caller:
    """The caller of a route that starts a run, authorized before the request body is validated: 401, then
    403 for a user who may not run, and only then 422 (`authorize_run_caller`)."""
    authorize_run_caller(caller)
    return caller


RunCallerDep = Annotated[Caller, Depends(get_run_caller)]


def client_ip(request: Request, services: Services) -> str | None:
    """The run's observed source address: null behind a platform proxy, where a visitor is on the internet."""
    return request_ingress(request, services).client_ip
