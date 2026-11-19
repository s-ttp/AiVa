"""Caller profile and the Control Center's sign-ins (ADR-0007, ADR-0010, ADR-0019).

`GET /v1/auth/me` works with any credential, so the UI learns roles and permissions from the gateway
whichever issuer signed the token (the POC HS256 issuer today, OIDC later). A request without a credential
is refused, behind the platform proxy too.

A browser without a sign-in starts a public demo session (`POST /v1/auth/public/session`): a two-hour
token for the seeded visitor, with its own pseudonym, issued only through the platform proxy or in the local
and test environments, and rate-limited. Two sign-ins mint a one-hour user JWT for a seeded demo user:
- passcode (`/v1/auth/demo/*`), for deployed gateways: enabled by `AIVA_DEMO_PASSCODE`, rate-limited with
  lockout and audited (`auth.demo_sign_in`);
- password-less dev (`/v1/auth/dev/*`): only when `AIVA_DEV_SIGN_IN=true` in the local or test environment.
The public visitor is never listed or issued a token by either.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aiva_gateway.api.deps import CallerDep, ServicesDep, SessionDep, request_ingress
from aiva_gateway.api.errors import ERROR_RESPONSES
from aiva_gateway.auth import demo_sign_in, public_session
from aiva_gateway.auth.authenticate import PUBLIC_VISITOR_ID
from aiva_gateway.auth.ingress import client_reference
from aiva_gateway.auth.rbac import GRANTS
from aiva_gateway.auth.tokens import decode_user_token, mint_user_token, new_session_id, pseudonymise
from aiva_gateway.config import LOCAL_ENVIRONMENTS
from aiva_gateway.contracts.common import ErrorEnvelope
from aiva_gateway.contracts.session import (
    CallerProfile,
    DemoTokenRequest,
    DemoUser,
    DevToken,
    DevTokenRequest,
    PublicSession,
    SignInOptions,
)
from aiva_gateway.core.enums import Role
from aiva_gateway.core.errors import AuthenticationError, NotFoundError, PermissionDeniedError
from aiva_gateway.db.models import PrincipalRow
from aiva_gateway.services import Services

router = APIRouter(tags=["session"])

DEV_TOKEN_TTL_SECONDS = 3600
_DEV_RESPONSES: dict[int | str, dict[str, object]] = {
    404: {**ERROR_RESPONSES[403], "description": "Dev sign-in is disabled or the user is unknown."}
}
_DEMO_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorEnvelope, "description": "Wrong passcode (DEMO_SIGN_IN_FAILED)."},
    404: {"model": ErrorEnvelope, "description": "Passcode sign-in is disabled, or the user is unknown."},
    422: ERROR_RESPONSES[422],
    429: {"model": ErrorEnvelope, "description": "Too many attempts (DEMO_SIGN_IN_LOCKED, Retry-After)."},
    503: {"model": ErrorEnvelope, "description": "The rate-limit store is unavailable; failed closed."},
}


def _require_dev_sign_in(services: Services) -> None:
    # Settings refuse dev sign-in outside local and test at start-up; checked again here.
    if not services.settings.dev_sign_in_allowed:
        raise NotFoundError(
            "Demo sign-in is not enabled on this gateway.",
            code="DEV_SIGN_IN_DISABLED",
            remediation="Sign in with the demo passcode, or set AIVA_DEV_SIGN_IN=true on a local gateway.",
        )


def _require_passcode(services: Services) -> str:
    passcode = services.settings.demo_passcode
    if passcode is None:
        raise NotFoundError(
            "Passcode sign-in is not enabled on this gateway.",
            code="DEMO_SIGN_IN_DISABLED",
            remediation="Set AIVA_DEMO_PASSCODE on the gateway.",
        )
    return passcode.get_secret_value()


async def _demo_users(session: AsyncSession, services: Services) -> list[DemoUser]:
    rows = (
        await session.execute(
            select(PrincipalRow)
            .where(PrincipalRow.tenant_id == services.catalog.tenant.tenant.id)
            .order_by(PrincipalRow.id)
        )
    ).scalars()
    return [
        DemoUser(user_id=r.id, display_name=r.display_name, roles=[Role(role) for role in r.roles])
        for r in rows
        if Role.PUBLIC_VISITOR.value not in r.roles
    ]


async def _demo_principal(session: AsyncSession, services: Services, user_id: str) -> PrincipalRow | None:
    principal = await session.get(PrincipalRow, user_id)
    if (
        principal is None
        or principal.tenant_id != services.catalog.tenant.tenant.id
        or principal.id == PUBLIC_VISITOR_ID
        or Role.PUBLIC_VISITOR.value in principal.roles
    ):
        return None
    return principal


def _token(services: Services, principal: PrincipalRow) -> DevToken:
    token = mint_user_token(
        services.settings,
        user_id=principal.id,
        tenant_id=principal.tenant_id,
        ttl_seconds=DEV_TOKEN_TTL_SECONDS,
    )
    expires = decode_user_token(services.settings, token)["exp"]
    return DevToken(access_token=token, expires_at=datetime.fromtimestamp(expires, tz=UTC))


def _public_sessions_allowed(request: Request, services: Services) -> bool:
    """Only a request the platform proxy vouched for, or any request on a local or test gateway."""
    if services.settings.environment.lower() in LOCAL_ENVIRONMENTS:
        return True
    return request_ingress(request, services).proxied


def _source(request: Request, services: Services) -> str:
    """The rate-limit key of this request's source: a client reference, never the raw address."""
    ingress = request_ingress(request, services)
    address = ingress.attested_ip if ingress.proxied else ingress.client_ip
    if address is None:
        return demo_sign_in.UNATTESTED_SOURCE
    return client_reference(services.settings.pseudonym_key.get_secret_value(), address)


@router.get(
    "/v1/auth/me",
    response_model=CallerProfile,
    responses=ERROR_RESPONSES,
    summary="The authenticated caller with its roles and permissions",
)
async def me(caller: CallerDep, session: SessionDep) -> CallerProfile:
    if caller.kind == "application":
        return CallerProfile(
            kind="application",
            tenant_id=caller.tenant_id,
            application_id=caller.application_id,
            department_id=caller.department_id,
        )
    principal = await session.get(PrincipalRow, caller.user_id)
    return CallerProfile(
        kind="user",
        tenant_id=caller.tenant_id,
        user_id=caller.user_id,
        display_name=principal.display_name if principal else None,
        department_id=caller.department_id,
        roles=sorted(caller.roles),
        permissions=sorted(p.value for p, roles in GRANTS.items() if caller.has_any(*roles)),
    )


@router.get(
    "/v1/auth/sign-in-options",
    response_model=SignInOptions,
    summary="The sign-ins this gateway offers (no credential needed)",
)
async def sign_in_options(request: Request, services: ServicesDep) -> SignInOptions:
    settings = services.settings
    return SignInOptions(
        public=_public_sessions_allowed(request, services),
        passcode=settings.demo_passcode is not None,
        dev=settings.dev_sign_in_allowed,
    )


@router.post(
    "/v1/auth/public/session",
    response_model=PublicSession,
    status_code=201,
    responses={
        403: {"model": ErrorEnvelope, "description": "Not reached through the public demo's proxy."},
        429: {
            "model": ErrorEnvelope,
            "description": "Too many sessions (PUBLIC_SESSION_LIMITED, Retry-After).",
        },
        503: {"model": ErrorEnvelope, "description": "The rate-limit store is unavailable; failed closed."},
    },
    summary="Start a public demo session: a two-hour token for one pseudonymous visitor (ADR-0019)",
)
async def start_public_session(request: Request, services: ServicesDep, session: SessionDep) -> PublicSession:
    if not _public_sessions_allowed(request, services):
        raise PermissionDeniedError(
            "Public demo sessions are only issued through the public demo.",
            code="PUBLIC_SESSION_UNAVAILABLE",
            remediation="Open the public demo, or sign in.",
        )
    tenant_id = services.catalog.tenant.tenant.id
    visitor = await session.get(PrincipalRow, PUBLIC_VISITOR_ID)
    if visitor is None or visitor.tenant_id != tenant_id or Role.PUBLIC_VISITOR.value not in visitor.roles:
        raise PermissionDeniedError(
            "This gateway has no public demo visitor.",
            code="PUBLIC_SESSION_UNAVAILABLE",
            remediation="Seed the reference data (config/tenant.yaml), or sign in.",
        )
    source = _source(request, services)
    await public_session.admit_session(
        services.redis, tenant_id, source, limits=services.settings.public_session_limits
    )
    session_id = new_session_id()
    token = mint_user_token(
        services.settings,
        user_id=visitor.id,
        tenant_id=tenant_id,
        ttl_seconds=public_session.SESSION_TTL_SECONDS,
        session_id=session_id,
    )
    pseudonym = pseudonymise(services.settings.pseudonym_key.get_secret_value(), visitor.id, session_id)
    session.add(public_session.session_audit(tenant_id, pseudonym=pseudonym, source=source))
    await session.commit()
    expires = decode_user_token(services.settings, token)["exp"]
    return PublicSession(access_token=token, expires_at=datetime.fromtimestamp(expires, tz=UTC))


@router.get(
    "/v1/auth/demo/users",
    response_model=list[DemoUser],
    responses={404: _DEMO_RESPONSES[404]},
    summary="Seeded demo users available to the passcode sign-in (disabled unless AIVA_DEMO_PASSCODE is set)",
)
async def demo_users(services: ServicesDep, session: SessionDep) -> list[DemoUser]:
    _require_passcode(services)
    return await _demo_users(session, services)


@router.post(
    "/v1/auth/demo/token",
    response_model=DevToken,
    responses=_DEMO_RESPONSES,
    summary="Sign in a seeded demo user with the demo passcode (rate-limited, audited)",
)
async def demo_token(
    body: DemoTokenRequest, request: Request, services: ServicesDep, session: SessionDep
) -> DevToken:
    expected = _require_passcode(services)
    tenant_id = services.catalog.tenant.tenant.id
    source = _source(request, services)
    await demo_sign_in.admit_attempt(services.redis, tenant_id, source)
    principal = await _demo_principal(session, services, body.user_id)
    principal_ref = (
        pseudonymise(services.settings.pseudonym_key.get_secret_value(), principal.id) if principal else None
    )
    if not demo_sign_in.passcode_matches(body.passcode.get_secret_value(), expected):
        locked = await demo_sign_in.record_failure(services.redis, tenant_id, source)
        session.add(
            demo_sign_in.sign_in_audit(
                tenant_id, source=source, principal_ref=principal_ref, outcome="wrong-passcode", locked=locked
            )
        )
        await session.commit()
        raise AuthenticationError(
            "The passcode is not correct.",
            code="DEMO_SIGN_IN_FAILED",
            remediation="Check the demo passcode with the demo owner.",
        )
    await demo_sign_in.record_success(services.redis, tenant_id, source)
    if principal is None or principal_ref is None:
        session.add(
            demo_sign_in.sign_in_audit(tenant_id, source=source, principal_ref=None, outcome="unknown-user")
        )
        await session.commit()
        raise NotFoundError(
            "Unknown demo user.", remediation="Choose one of the users listed by /v1/auth/demo/users."
        )
    session.add(
        demo_sign_in.sign_in_audit(tenant_id, source=source, principal_ref=principal_ref, outcome="signed-in")
    )
    await session.commit()
    return _token(services, principal)


@router.get(
    "/v1/auth/dev/users",
    response_model=list[DemoUser],
    responses=_DEV_RESPONSES,
    summary="Seeded demo users available to the dev sign-in (local and test environments only)",
)
async def dev_users(services: ServicesDep, session: SessionDep) -> list[DemoUser]:
    _require_dev_sign_in(services)
    return await _demo_users(session, services)


@router.post(
    "/v1/auth/dev/token",
    response_model=DevToken,
    responses={**_DEV_RESPONSES, 422: ERROR_RESPONSES[422]},
    summary="Mint a short-lived user JWT for a seeded demo user (local and test environments only)",
)
async def dev_token(body: DevTokenRequest, services: ServicesDep, session: SessionDep) -> DevToken:
    _require_dev_sign_in(services)
    principal = await _demo_principal(session, services, body.user_id)
    if principal is None:
        raise NotFoundError(
            "Unknown demo user.", remediation="Choose one of the users listed by /v1/auth/dev/users."
        )
    return _token(services, principal)
