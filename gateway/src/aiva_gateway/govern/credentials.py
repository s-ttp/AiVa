"""Application keys issued and revoked from the admin console (ADR-0027).

A key is `aiva_<key_id>.<secret>`. Its secret is returned once, when it is issued, and only an HMAC is kept
(ADR-0007). A key can spend in live mode, so it always expires, an application holds a few at most, and
every issue and revoke is an `audit_log` row that never carries any part of the secret.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aiva_gateway.auth.credentials import new_credential
from aiva_gateway.auth.principal import Caller
from aiva_gateway.auth.rbac import Permission, granted
from aiva_gateway.contracts.govern import CredentialOut
from aiva_gateway.core.enums import Role
from aiva_gateway.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from aiva_gateway.db.base import utcnow
from aiva_gateway.db.models import ApplicationCredentialRow, ApplicationRow, AuditLogRow

# Active (neither revoked nor expired) keys an application may hold at once.
ACTIVE_LIMIT = 5
# Roles that read every application's keys; an application owner reads its own applications' only.
_READ_ALL = (Role.PLATFORM_ADMIN, Role.TENANT_ADMIN, Role.SECURITY_AUDITOR)


def is_active(row: ApplicationCredentialRow, now: datetime) -> bool:
    return row.revoked_at is None and (row.expires_at is None or row.expires_at > now)


def credential_out(row: ApplicationCredentialRow, now: datetime) -> CredentialOut:
    return CredentialOut(
        key_id=row.key_id,
        label=row.label,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
        created_by=row.created_by,
        revoked_by=row.revoked_by,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        active=is_active(row, now),
    )


def may_read(caller: Caller, application_id: str) -> bool:
    if not granted(caller, Permission.CREDENTIALS_READ):
        return False
    return caller.has_any(*_READ_ALL) or application_id in caller.owned_application_ids


def require_read(caller: Caller, application_id: str) -> None:
    if not may_read(caller, application_id):
        raise PermissionDeniedError(
            "You may not read this application's keys.",
            remediation="Ask an administrator, or the application's owner.",
        )


async def _application(
    session: AsyncSession, caller: Caller, application_id: str, *, lock: bool = False
) -> ApplicationRow:
    query = select(ApplicationRow).where(ApplicationRow.id == application_id)
    row = (await session.execute(query.with_for_update() if lock else query)).scalar_one_or_none()
    if row is None or row.tenant_id != caller.tenant_id:
        raise NotFoundError("Application not found.")
    return row


async def list_credentials(
    session: AsyncSession, caller: Caller, application_id: str
) -> Sequence[ApplicationCredentialRow]:
    await _application(session, caller, application_id)
    rows = await session.execute(
        select(ApplicationCredentialRow)
        .where(ApplicationCredentialRow.application_id == application_id)
        .order_by(ApplicationCredentialRow.created_at)
    )
    return rows.scalars().all()


def _audit(caller: Caller, action: str, row: ApplicationCredentialRow) -> AuditLogRow:
    return AuditLogRow(
        tenant_id=caller.tenant_id,
        actor_kind="user",
        actor_id=caller.actor_ref,
        action=action,
        target_type="application_credential",
        target_id=row.key_id,
        details={
            "application_id": row.application_id,
            "label": row.label,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        },
    )


async def issue(
    session: AsyncSession, caller: Caller, application_id: str, *, label: str, days: int, pepper: str
) -> tuple[str, ApplicationCredentialRow]:
    """Issue a key and return (plaintext, row). The caller's transaction commits both the key and its audit
    row. The application row is locked first, so concurrent issues cannot pass the active-key limit."""
    await _application(session, caller, application_id, lock=True)
    now = utcnow()
    existing = await session.execute(
        select(ApplicationCredentialRow).where(ApplicationCredentialRow.application_id == application_id)
    )
    if sum(is_active(row, now) for row in existing.scalars()) >= ACTIVE_LIMIT:
        raise ConflictError(
            f"This application already has {ACTIVE_LIMIT} active keys.",
            code="CREDENTIAL_LIMIT",
            remediation="Revoke a key the application no longer uses, then issue a new one.",
        )
    key_id, plaintext, secret_hmac = new_credential(pepper)
    row = ApplicationCredentialRow(
        key_id=key_id,
        application_id=application_id,
        secret_hmac=secret_hmac,
        label=label,
        created_at=now,
        created_by=caller.actor_ref,
        expires_at=now + timedelta(days=days),
    )
    session.add(row)
    session.add(_audit(caller, "credential.create", row))
    return plaintext, row


async def revoke(
    session: AsyncSession, caller: Caller, application_id: str, key_id: str
) -> ApplicationCredentialRow:
    """Revoke a key of this application. Revoking a revoked key changes nothing and writes no audit row."""
    await _application(session, caller, application_id)
    row = (
        await session.execute(
            select(ApplicationCredentialRow)
            .where(
                ApplicationCredentialRow.key_id == key_id,
                ApplicationCredentialRow.application_id == application_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("Key not found for this application.")
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        row.revoked_by = caller.actor_ref
        session.add(_audit(caller, "credential.revoke", row))
    return row
