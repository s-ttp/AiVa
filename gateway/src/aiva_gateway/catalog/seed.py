"""Upsert `config/` reference data into Postgres and publish the policy bundle. Idempotent."""

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aiva_gateway.auth.credentials import new_credential
from aiva_gateway.auth.tokens import pseudonymise
from aiva_gateway.core.money import usd_to_micros
from aiva_gateway.db import models as db
from aiva_gateway.db.base import Base
from aiva_gateway.govern.publisher import publish_policies
from aiva_gateway.services import Services


async def _upsert(
    session: AsyncSession, model: type[Base], rows: list[dict[str, object]], keys: list[str]
) -> None:
    if not rows:
        return
    statement = insert(model).values(rows)
    updates = {c: statement.excluded[c] for c in rows[0] if c not in keys}
    await session.execute(statement.on_conflict_do_update(index_elements=keys, set_=updates))


async def seed_reference_data(services: Services, session: AsyncSession) -> None:
    catalog, settings = services.catalog, services.settings
    tenant = catalog.tenant
    tid = tenant.tenant.id
    await _upsert(session, db.TenantRow, [{"id": tid, "name": tenant.tenant.name, "currency": "USD"}], ["id"])
    await _upsert(
        session,
        db.CostCenterRow,
        [{"id": c.id, "tenant_id": tid, "code": c.code, "name": c.name} for c in tenant.cost_centers],
        ["id"],
    )
    await _upsert(
        session,
        db.DepartmentRow,
        [
            {
                "id": d.id,
                "tenant_id": tid,
                "name": d.name,
                "business_function": d.business_function,
                "cost_center_id": d.cost_center_id,
            }
            for d in tenant.departments
        ],
        ["id"],
    )
    await _upsert(
        session,
        db.TrustedNetworkRow,
        [{"id": n.id, "tenant_id": tid, "name": n.name, "cidr": n.cidr} for n in tenant.trusted_networks],
        ["id"],
    )
    await _upsert(
        session,
        db.ApplicationRow,
        [
            {
                "id": a.id,
                "tenant_id": tid,
                "department_id": a.department_id,
                "name": a.name,
                "enabled": a.enabled,
                "trusted_network_ids": a.trusted_network_ids,
                "allowed_tasks": a.allowed_tasks,
                "allowed_models": a.allowed_models,
                "classification_floor": a.classification_floor.value,
            }
            for a in tenant.applications
        ],
        ["id"],
    )
    key = settings.pseudonym_key.get_secret_value()
    await _upsert(
        session,
        db.PrincipalRow,
        [
            {
                "id": u.id,
                "tenant_id": tid,
                "pseudonym": pseudonymise(key, u.id),
                "display_name": u.display_name,
                "roles": [r.value for r in u.roles],
                "department_id": u.department_id,
                "owned_application_ids": u.owned_application_ids,
            }
            for u in tenant.users
        ],
        ["id"],
    )
    await _upsert(
        session,
        db.ModelRow,
        [
            {
                "id": m.id,
                "display_name": m.display_name,
                "role": m.role,
                "provider": m.provider,
                "provider_model_id": m.provider_model_id,
                "revision": m.revision,
                "sovereign": m.sovereign,
                "external": m.external,
                "enabled": True,
                "rate_card_id": m.rate_card_id,
                "spec": m.model_dump(mode="json"),
            }
            for m in catalog.models.models
        ],
        ["id"],
    )
    await _upsert(
        session,
        db.RateCardRow,
        [
            {
                "id": rc.id,
                "version": catalog.rate_cards.version,
                "model_id": rc.model_id,
                "currency": "USD",
                "input_micros_per_mtok": usd_to_micros(rc.input_per_mtok),
                "output_micros_per_mtok": usd_to_micros(rc.output_per_mtok),
                "cached_input_micros_per_mtok": usd_to_micros(rc.cached_input_per_mtok),
                "gpu_micros_per_hour": usd_to_micros(rc.gpu_per_hour)
                if rc.gpu_per_hour is not None
                else None,
                "effective_from": rc.effective_from,
                "source": rc.source,
            }
            for rc in catalog.rate_cards.rate_cards
        ],
        ["id"],
    )
    await _upsert(
        session,
        db.BudgetRow,
        [
            {
                "id": f"bud-{b.scope_type}-{b.scope_id}-{b.period}",
                "tenant_id": tid,
                "scope_type": b.scope_type,
                "scope_id": b.scope_id,
                "period": b.period,
                "cap_micros": usd_to_micros(b.cap_usd),
                "alert_thresholds": b.alert_thresholds,
                "on_exhaustion": b.on_exhaustion,
            }
            for b in tenant.budgets
        ],
        ["id"],
    )
    await _upsert(
        session,
        db.TaxonomyVersionRow,
        [{"version": catalog.taxonomy.version, "definition": catalog.taxonomy.model_dump(mode="json")}],
        ["version"],
    )
    await publish_policies(
        services, session, catalog.policies, actor="seed", notes="Seeded from config/policies"
    )


async def create_application_credential(
    services: Services, session: AsyncSession, application_id: str, label: str
) -> str:
    """Create a credential and return the plaintext. It is shown once and never stored."""
    if services.catalog.application(application_id) is None:
        raise ValueError(f"unknown application {application_id}")
    key_id, plaintext, secret_hmac = new_credential(services.settings.credential_pepper.get_secret_value())
    session.add(
        db.ApplicationCredentialRow(
            key_id=key_id, application_id=application_id, secret_hmac=secret_hmac, label=label
        )
    )
    return plaintext
