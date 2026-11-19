"""Policy version lifecycle and OPA synchronisation (ADR-0005)."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aiva_gateway.catalog.models import PolicyDocument
from aiva_gateway.core.canonical import sha256_hex
from aiva_gateway.db.models import AuditLogRow, PolicyVersionRow
from aiva_gateway.govern.bundle import build_bundle
from aiva_gateway.services import Services


async def latest_published(session: AsyncSession, tenant_id: str) -> PolicyVersionRow | None:
    return (
        await session.execute(
            select(PolicyVersionRow)
            .where(PolicyVersionRow.tenant_id == tenant_id, PolicyVersionRow.status == "published")
            .order_by(PolicyVersionRow.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def publish_policies(
    services: Services, session: AsyncSession, policies: list[PolicyDocument], *, actor: str, notes: str
) -> PolicyVersionRow:
    """Create and publish a new version unless it is identical to the current published bundle."""
    tenant_id = services.catalog.tenant.tenant.id
    current = await latest_published(session, tenant_id)
    highest = await session.scalar(
        select(func.max(PolicyVersionRow.version)).where(PolicyVersionRow.tenant_id == tenant_id)
    )
    next_version = (highest or 0) + 1
    bundle = build_bundle(
        services.catalog,
        policies,
        version=str(next_version),
        allow_simulated_ingress=services.settings.allow_simulated_ingress,
    )
    if current is not None and _content_hash(current.bundle) == _content_hash(bundle):
        return current
    now = datetime.now(UTC)
    if current is not None:
        current.status = "archived"
    row = PolicyVersionRow(
        tenant_id=tenant_id,
        version=next_version,
        status="published",
        bundle=bundle,
        bundle_hash=bundle["meta"]["bundle_hash"],
        notes=notes,
        created_by=actor,
        created_at=now,
        published_by=actor,
        published_at=now,
    )
    session.add(row)
    session.add(
        AuditLogRow(
            tenant_id=tenant_id,
            actor_kind="user",
            actor_id=actor,
            action="policy.publish",
            target_type="policy_version",
            target_id=str(next_version),
            details={"bundle_hash": row.bundle_hash, "notes": notes},
        )
    )
    await session.flush()
    await services.opa.publish_bundle(bundle)
    return row


async def ensure_opa_bundle(services: Services, session: AsyncSession) -> str | None:
    """Push the published bundle to OPA if OPA lost it (restart) or holds another version."""
    current = await latest_published(session, services.catalog.tenant.tenant.id)
    if current is None:
        return None
    if await services.opa.loaded_bundle_hash() != current.bundle_hash:
        await services.opa.publish_bundle(current.bundle)
    return current.bundle_hash


def _content_hash(bundle: dict[str, Any]) -> str:
    """Hash of the bundle ignoring its version number, to detect no-op publishes."""
    meta = {k: v for k, v in bundle["meta"].items() if k not in {"version", "bundle_hash"}}
    return sha256_hex({**{k: v for k, v in bundle.items() if k != "meta"}, "meta": meta})
