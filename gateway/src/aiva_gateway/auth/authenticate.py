"""Resolve a bearer credential to a Caller: an application credential, or a user JWT (a public demo
session's token included, ADR-0019)."""

from datetime import timedelta

import jwt
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aiva_gateway.auth.credentials import parse_credential, verify_secret
from aiva_gateway.auth.principal import Caller
from aiva_gateway.auth.tokens import decode_user_token, pseudonymise
from aiva_gateway.config import Settings
from aiva_gateway.core.enums import Role
from aiva_gateway.core.errors import AuthenticationError
from aiva_gateway.db.base import utcnow
from aiva_gateway.db.models import ApplicationCredentialRow, ApplicationRow, PrincipalRow

_INVALID = "Invalid or missing credentials."
# The one seeded principal every public visitor acts as (config/tenant.yaml, ADR-0019).
PUBLIC_VISITOR_ID = "u-public-visitor"
# A key's last use is recorded at most this often, so authenticating stays a read (ADR-0027).
LAST_USED_RESOLUTION = timedelta(hours=1)


async def authenticate(authorization: str | None, session: AsyncSession, settings: Settings) -> Caller:
    if not authorization or not authorization.startswith("Bearer "):
        raise AuthenticationError(_INVALID, remediation="Send 'Authorization: Bearer <credential>'.")
    token = authorization.removeprefix("Bearer ").strip()
    parsed = parse_credential(token)
    if parsed:
        return await _application_caller(*parsed, session=session, settings=settings)
    return await _user_caller(token, session=session, settings=settings)


async def _application_caller(
    key_id: str, secret: str, *, session: AsyncSession, settings: Settings
) -> Caller:
    row = (
        await session.execute(
            select(ApplicationCredentialRow, ApplicationRow)
            .join(ApplicationRow, ApplicationRow.id == ApplicationCredentialRow.application_id)
            .where(ApplicationCredentialRow.key_id == key_id)
        )
    ).first()
    if row is None:
        raise AuthenticationError(_INVALID)
    credential, application = row
    pepper = settings.credential_pepper.get_secret_value()
    if credential.revoked_at is not None or not verify_secret(secret, credential.secret_hmac, pepper):
        raise AuthenticationError(_INVALID)
    now = utcnow()
    # A revoked or expired key is refused with the same answer as an unknown one.
    if credential.expires_at is not None and credential.expires_at <= now:
        raise AuthenticationError(_INVALID)
    if credential.last_used_at is None or credential.last_used_at <= now - LAST_USED_RESOLUTION:
        await session.execute(
            update(ApplicationCredentialRow)
            .where(
                ApplicationCredentialRow.key_id == credential.key_id,
                or_(
                    ApplicationCredentialRow.last_used_at.is_(None),
                    ApplicationCredentialRow.last_used_at <= now - LAST_USED_RESOLUTION,
                ),
            )
            .values(last_used_at=now)
        )
        await session.commit()
    return Caller(
        kind="application",
        tenant_id=application.tenant_id,
        application_id=application.id,
        credential_id=credential.key_id,
        department_id=application.department_id,
    )


async def _user_caller(token: str, *, session: AsyncSession, settings: Settings) -> Caller:
    try:
        claims = decode_user_token(settings, token)
    except jwt.PyJWTError as exc:
        raise AuthenticationError(_INVALID) from exc
    principal = await session.get(PrincipalRow, claims["sub"])
    if principal is None or principal.tenant_id != claims["tid"]:
        raise AuthenticationError(_INVALID)
    session_id = claims.get("sid")
    visitor = Role.PUBLIC_VISITOR.value in principal.roles
    # A public demo session's token, and only such a token, names the visitor and carries a session id.
    if visitor != (principal.id == PUBLIC_VISITOR_ID) or visitor != isinstance(session_id, str):
        raise AuthenticationError(_INVALID)
    return Caller(
        kind="user",
        tenant_id=principal.tenant_id,
        user_id=principal.id,
        pseudonym=pseudonymise(
            settings.pseudonym_key.get_secret_value(), principal.id, session_id if visitor else None
        ),
        roles=frozenset(Role(r) for r in principal.roles),
        department_id=principal.department_id,
        owned_application_ids=frozenset(principal.owned_application_ids),
    )
