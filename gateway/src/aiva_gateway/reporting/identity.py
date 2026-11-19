"""Authorized identity resolution (LLD §21.8): pseudonym -> identity, separately permissioned and audited.

Reports, exports and drilldowns only ever carry pseudonyms. Resolving one requires `reports:identity`
(tenant administrators and security auditors), a stated reason, and writes an append-only audit row listing
exactly which pseudonyms were resolved. Only the caller's own tenant is searched.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aiva_gateway.auth.principal import Caller
from aiva_gateway.auth.tokens import pseudonymise
from aiva_gateway.catalog import Catalog
from aiva_gateway.config import Settings
from aiva_gateway.contracts.reports import IdentityResolution, IdentityResolutionRequest, ResolvedIdentity
from aiva_gateway.db.models import AuditLogRow, PrincipalRow
from aiva_gateway.reporting.dimensions import SERVICE_PREFIX
from aiva_gateway.reporting.scope import pseudonym_of
from aiva_gateway.reporting.traffic import SYNTHETIC_USERS


async def resolve_identities(
    session: AsyncSession,
    settings: Settings,
    catalog: Catalog,
    caller: Caller,
    body: IdentityResolutionRequest,
) -> IdentityResolution:
    """Resolve and audit. The caller commits the session (the audit row and nothing else is written)."""
    actor = pseudonym_of(caller)
    wanted = list(dict.fromkeys(body.pseudonyms))
    rows = await session.scalars(
        select(PrincipalRow).where(
            PrincipalRow.tenant_id == caller.tenant_id, PrincipalRow.pseudonym.in_(wanted)
        )
    )
    directory = {row.pseudonym: row for row in rows}
    key = settings.pseudonym_key.get_secret_value()
    synthetic = {pseudonymise(key, user.user_id): user for user in SYNTHETIC_USERS}
    identities = []
    for pseudonym in wanted:
        if pseudonym in directory:
            principal = directory[pseudonym]
            identities.append(
                ResolvedIdentity(
                    pseudonym=pseudonym,
                    kind="directory",
                    user_id=principal.id,
                    display_name=principal.display_name,
                )
            )
        elif pseudonym in synthetic:
            user = synthetic[pseudonym]
            identities.append(
                ResolvedIdentity(
                    pseudonym=pseudonym,
                    kind="synthetic",
                    user_id=user.user_id,
                    display_name=f"Synthetic traffic user {user.user_id} (generated, not a person)",
                )
            )
        elif pseudonym.startswith(SERVICE_PREFIX):
            app = catalog.application(pseudonym.removeprefix(SERVICE_PREFIX))
            identities.append(
                ResolvedIdentity(
                    pseudonym=pseudonym,
                    kind="service" if app else "unknown",
                    display_name=f"Service principal · {app.name}" if app else None,
                )
            )
        else:
            identities.append(ResolvedIdentity(pseudonym=pseudonym, kind="unknown"))
    session.add(
        AuditLogRow(
            tenant_id=caller.tenant_id,
            actor_kind="user",
            actor_id=actor,
            action="report.identity_resolve",
            target_type="principal",
            target_id=wanted[0] if len(wanted) == 1 else f"{len(wanted)} pseudonyms",
            details={
                "pseudonyms": wanted,
                "reason": body.reason,
                "resolved": sum(1 for i in identities if i.kind in ("directory", "synthetic")),
            },
        )
    )
    return IdentityResolution(identities=identities)
