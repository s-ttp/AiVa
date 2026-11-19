"""AiVa Govern administration (LLD §22.5): applications, trusted networks, models and tools, and the
applications' keys (ADR-0027).

Grants come from the published policy bundle, i.e. what the policy engine enforces now, enriched with
display names from the catalog. A key's secret is returned once, when it is issued, and never again.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from aiva_gateway.api.deps import CallerDep, ServicesDep, SessionDep
from aiva_gateway.api.errors import ERROR_RESPONSES
from aiva_gateway.auth.principal import Caller
from aiva_gateway.auth.rbac import Permission, granted, require
from aiva_gateway.catalog.models import ModelSpec, PolicyMatch
from aiva_gateway.contracts.govern import (
    ApplicationList,
    ApplicationOut,
    CredentialCreate,
    CredentialIssued,
    CredentialList,
    CredentialOut,
    IngressConfigOut,
    ModelAllowlist,
    ModelOut,
    ModelRateCardOut,
    PolicyAllowlistOut,
    ToolList,
    ToolOut,
    TrustedNetworkList,
    TrustedNetworkOut,
    TrustedNetworkRef,
)
from aiva_gateway.core.enums import Classification
from aiva_gateway.core.errors import DependencyUnavailableError, NotFoundError
from aiva_gateway.db.base import utcnow
from aiva_gateway.db.models import PolicyVersionRow
from aiva_gateway.govern import credentials as keys
from aiva_gateway.govern.model_switch import switched_off
from aiva_gateway.govern.publisher import latest_published
from aiva_gateway.services import Services

router = APIRouter(tags=["govern"])
_RESPONSES = {**ERROR_RESPONSES, 503: ERROR_RESPONSES[403]}

INVARIANTS = [
    "Confidential, restricted and sovereign-only data uses only sovereign models (AIPEL-INV-001).",
    "Confidential, restricted and sovereign-only data never reaches CIM or external providers.",
    "Confidential, restricted and sovereign-only data never reaches an external tool (AIPEL-INV-002).",
    "Routing mode local-only excludes external models and tools.",
]


async def _published(session: AsyncSession, tenant_id: str) -> PolicyVersionRow:
    row = await latest_published(session, tenant_id)
    if row is None:
        raise DependencyUnavailableError(
            "No policy version is published.",
            code="POLICY_BUNDLE_MISSING",
            remediation="Publish a policy version in AiVa Govern.",
        )
    return row


def _network_refs(
    services: Services, bundle: dict[str, Any], network_ids: list[str]
) -> list[TrustedNetworkRef]:
    names = {n.id: n.name for n in services.catalog.tenant.trusted_networks}
    return [
        TrustedNetworkRef(id=nid, name=names.get(nid, nid), cidr=bundle["networks"][nid])
        for nid in network_ids
        if nid in bundle["networks"]
    ]


async def _application(
    services: Services,
    session: AsyncSession,
    caller: Caller,
    bundle: dict[str, Any],
    app_id: str,
    grants: dict[str, Any],
) -> ApplicationOut:
    catalog_app = services.catalog.application(app_id)
    # Key metadata only for callers who may read this application's keys (ADR-0027).
    credentials = (
        await keys.list_credentials(session, caller, app_id) if keys.may_read(caller, app_id) else []
    )
    now = utcnow()
    return ApplicationOut(
        id=app_id,
        name=catalog_app.name if catalog_app else app_id,
        department_id=grants["department_id"],
        enabled=grants["enabled"],
        classification_floor=Classification(grants["classification_floor"]),
        allowed_tasks=grants["allowed_tasks"],
        allowed_models=grants["allowed_models"],
        allowed_tools=grants.get("allowed_tools", []),
        trusted_networks=_network_refs(services, bundle, grants["trusted_network_ids"]),
        internet_callers=grants.get("internet_callers", False),
        switched_off_models=switched_off(bundle, app_id),
        credentials=[keys.credential_out(c, now) for c in credentials],
    )


@router.get(
    "/v1/admin/applications",
    response_model=ApplicationList,
    responses=_RESPONSES,
    summary="Applications & Identities: grants enforced by the published policy version",
)
async def list_applications(caller: CallerDep, session: SessionDep, services: ServicesDep) -> ApplicationList:
    require(caller, Permission.POLICIES_READ)
    row = await _published(session, caller.tenant_id)
    bundle = row.bundle
    return ApplicationList(
        policy_version=str(row.version),
        bundle_hash=row.bundle_hash,
        viewer_can_switch=granted(caller, Permission.POLICIES_WRITE),
        applications=[
            await _application(services, session, caller, bundle, app_id, grants)
            for app_id, grants in sorted(bundle["applications"].items())
        ],
    )


@router.get(
    "/v1/admin/applications/{application_id}",
    response_model=ApplicationOut,
    responses={**_RESPONSES, 404: ERROR_RESPONSES[403]},
    summary="One application with its grants and credential metadata",
)
async def get_application(
    application_id: str, caller: CallerDep, session: SessionDep, services: ServicesDep
) -> ApplicationOut:
    require(caller, Permission.POLICIES_READ)
    bundle = (await _published(session, caller.tenant_id)).bundle
    grants = bundle["applications"].get(application_id)
    if grants is None:
        raise NotFoundError("Application not found.")
    return await _application(services, session, caller, bundle, application_id, grants)


# --- Application keys (ADR-0027) ------------------------------------------------------------------


async def get_key_manager(caller: CallerDep) -> Caller:
    """An administrator, authorized before the request body is validated (401, then 403, then 422)."""
    require(caller, Permission.CREDENTIALS_MANAGE)
    return caller


KeyManagerDep = Annotated[Caller, Depends(get_key_manager)]
_KEY_RESPONSES = {**ERROR_RESPONSES, 404: ERROR_RESPONSES[403], 409: ERROR_RESPONSES[403]}


@router.get(
    "/v1/admin/applications/{application_id}/credentials",
    response_model=CredentialList,
    responses=_KEY_RESPONSES,
    summary="An application's keys: metadata only, never a secret",
)
async def list_credentials(application_id: str, caller: CallerDep, session: SessionDep) -> CredentialList:
    keys.require_read(caller, application_id)
    rows = await keys.list_credentials(session, caller, application_id)
    now = utcnow()
    manage = granted(caller, Permission.CREDENTIALS_MANAGE)
    active = sum(keys.is_active(row, now) for row in rows)
    return CredentialList(
        application_id=application_id,
        active_limit=keys.ACTIVE_LIMIT,
        viewer_can_issue=manage and active < keys.ACTIVE_LIMIT,
        viewer_can_revoke=manage,
        credentials=[keys.credential_out(row, now) for row in rows],
    )


@router.post(
    "/v1/admin/applications/{application_id}/credentials",
    response_model=CredentialIssued,
    status_code=201,
    responses=_KEY_RESPONSES,
    summary="Issue a key: its secret is returned this once, and the key expires",
)
async def create_credential(
    application_id: str,
    body: CredentialCreate,
    response: Response,
    caller: KeyManagerDep,
    services: ServicesDep,
) -> CredentialIssued:
    async with services.sessionmaker() as session, session.begin():
        plaintext, row = await keys.issue(
            session,
            caller,
            application_id,
            label=body.label,
            days=body.expires_in_days,
            pepper=services.settings.credential_pepper.get_secret_value(),
        )
    response.headers["Cache-Control"] = "no-store"
    return CredentialIssued(
        application_id=application_id, credential=plaintext, metadata=keys.credential_out(row, utcnow())
    )


@router.post(
    "/v1/admin/applications/{application_id}/credentials/{key_id}/revoke",
    response_model=CredentialOut,
    responses=_KEY_RESPONSES,
    summary="Revoke a key: it stops authenticating at once",
)
async def revoke_credential(
    application_id: str, key_id: str, caller: KeyManagerDep, services: ServicesDep
) -> CredentialOut:
    async with services.sessionmaker() as session, session.begin():
        row = await keys.revoke(session, caller, application_id, key_id)
    return keys.credential_out(row, utcnow())


@router.get(
    "/v1/admin/trusted-networks",
    response_model=TrustedNetworkList,
    responses=_RESPONSES,
    summary="Trusted Networks: CIDRs, the applications using them and ingress configuration",
)
async def list_trusted_networks(
    caller: CallerDep, session: SessionDep, services: ServicesDep
) -> TrustedNetworkList:
    require(caller, Permission.POLICIES_READ)
    row = await _published(session, caller.tenant_id)
    bundle = row.bundle
    names = {n.id: n.name for n in services.catalog.tenant.trusted_networks}
    settings = services.settings
    return TrustedNetworkList(
        policy_version=str(row.version),
        bundle_hash=row.bundle_hash,
        ingress=IngressConfigOut(
            trusted_proxies=list(settings.trusted_proxies),
            forwarded_for_trusted=bool(settings.trusted_proxies),
            simulated_ingress_allowed=bool(bundle["meta"].get("allow_simulated_ingress")),
        ),
        networks=[
            TrustedNetworkOut(
                id=nid,
                name=names.get(nid, nid),
                cidr=cidr,
                application_ids=sorted(
                    app_id
                    for app_id, grants in bundle["applications"].items()
                    if nid in grants["trusted_network_ids"]
                ),
            )
            for nid, cidr in sorted(bundle["networks"].items())
        ],
    )


def _rate_card(services: Services, spec: ModelSpec) -> ModelRateCardOut | None:
    """The model's pinned price, in the same integer micro-USD the cost paths use (PriceBook)."""
    card = services.catalog.rate_card(spec.rate_card_id)
    if card is None:
        return None
    price = services.prices.price(spec.id)
    return ModelRateCardOut(
        id=card.id,
        version=services.catalog.rate_cards.version,
        currency=services.catalog.rate_cards.currency,
        input_micros_per_mtok=price.input_micros_per_mtok,
        output_micros_per_mtok=price.output_micros_per_mtok,
        cached_input_micros_per_mtok=price.cached_input_micros_per_mtok,
        gpu_micros_per_hour=price.gpu_micros_per_hour,
        effective_from=card.effective_from,
        source=card.source,
    )


@router.get(
    "/v1/admin/models",
    response_model=ModelAllowlist,
    responses=_RESPONSES,
    summary="Model Allowlist: registry, per-application grants and policy allowlists",
)
async def model_allowlist(caller: CallerDep, session: SessionDep, services: ServicesDep) -> ModelAllowlist:
    require(caller, Permission.POLICIES_READ)
    row = await _published(session, caller.tenant_id)
    bundle = row.bundle
    models = []
    for model_id, flags in sorted(bundle["models"].items()):
        spec = services.catalog.model(model_id)
        models.append(
            ModelOut(
                id=model_id,
                display_name=spec.display_name if spec else model_id,
                role=flags["role"],
                catalog_configured=spec is not None,
                provider=spec.provider if spec else None,
                provider_model_id=spec.provider_model_id if spec else None,
                revision=spec.revision if spec else None,
                region=spec.region if spec else None,
                context_window=spec.context_window if spec else None,
                provider_route=spec.provider_route if spec else None,
                adapter=spec.adapter if spec else None,
                rate_card=_rate_card(services, spec) if spec else None,
                sovereign=flags["sovereign"],
                external=flags["external"],
                enabled=flags["enabled"],
                application_ids=sorted(
                    app_id
                    for app_id, grants in bundle["applications"].items()
                    if model_id in grants["allowed_models"]
                ),
                switched_off_application_ids=sorted(
                    app_id for app_id in bundle["applications"] if model_id in switched_off(bundle, app_id)
                ),
            )
        )
    allowlists = [
        PolicyAllowlistOut(
            policy_id=p["policy_id"],
            title=p.get("title", p["policy_id"]),
            match=PolicyMatch.model_validate(p["match"]),
            allowed_models=p["enforce"].get("allowed_models"),
            execution_path=p["enforce"].get("execution_path"),
            external_providers=p["enforce"].get("external_providers"),
            cim=p["enforce"].get("cim"),
        )
        for p in bundle["policies"]
        if any(k in p["enforce"] for k in ("allowed_models", "execution_path", "external_providers", "cim"))
    ]
    return ModelAllowlist(
        policy_version=str(row.version),
        bundle_hash=row.bundle_hash,
        viewer_can_switch=granted(caller, Permission.POLICIES_WRITE),
        models=models,
        policy_allowlists=allowlists,
        invariants=INVARIANTS,
    )


@router.get(
    "/v1/admin/tools",
    response_model=ToolList,
    responses=_RESPONSES,
    summary="Tool Permissions: registry and per-application grants",
)
async def list_tools(caller: CallerDep, session: SessionDep, services: ServicesDep) -> ToolList:
    require(caller, Permission.POLICIES_READ)
    row = await _published(session, caller.tenant_id)
    bundle = row.bundle
    tools = []
    for tool_id, flags in sorted(bundle.get("tools", {}).items()):
        spec = services.catalog.tool(tool_id)
        tools.append(
            ToolOut(
                id=tool_id,
                display_name=spec.display_name if spec else tool_id,
                description=spec.description if spec else "",
                access=flags["access"],
                egress=flags["egress"],
                enabled=flags["enabled"],
                application_ids=sorted(
                    app_id
                    for app_id, grants in bundle["applications"].items()
                    if tool_id in grants.get("allowed_tools", [])
                ),
            )
        )
    return ToolList(policy_version=str(row.version), bundle_hash=row.bundle_hash, tools=tools)
