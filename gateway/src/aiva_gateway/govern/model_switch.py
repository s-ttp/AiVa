"""Model switches per application (ADR-0027 §2): turn a granted model off, or back on, for one application.

A switch is a policy document, `GCT-SWITCH-<application_id>`, matching that application with `allowed_models`
set to its granted models minus the ones switched off. The policy engine intersects every matched allowlist,
so the document can only narrow. A switch creates a draft from the published version; testing and publishing
it are the existing lifecycle steps. Switching off only narrows; switching back on widens, so `impact.py`
makes it high-impact and a different administrator must publish it.

When the sovereign model is switched off, the application's confidential, restricted and sovereign-only
requests have no eligible model: they are blocked by AI policy (AIPEL-ROUTE-001), never rerouted (Appendix B).
The response simulates the draft for every classification the application's requests can have and says so.
"""

import ipaddress
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from aiva_gateway.auth.principal import Caller
from aiva_gateway.catalog.models import Application, PolicyDocument, PolicyEnforce, PolicyMatch
from aiva_gateway.contracts.messages import ChatMessage
from aiva_gateway.contracts.policy import (
    DecisionReason,
    ModelSwitchCheck,
    ModelSwitchRequest,
    ModelSwitchResponse,
    PolicySample,
)
from aiva_gateway.core.enums import Classification
from aiva_gateway.core.errors import (
    ConflictError,
    DependencyUnavailableError,
    NotFoundError,
    ValidationFailedError,
)
from aiva_gateway.db.models import AuditLogRow, PolicyVersionRow
from aiva_gateway.govern import lifecycle
from aiva_gateway.govern.publisher import latest_published
from aiva_gateway.services import Services

SWITCH_PREFIX = "GCT-SWITCH-"
# Allowlists intersect, so a switch's priority does not change what it allows; it orders the policy list.
SWITCH_PRIORITY = 600
ROUTE_RULE = "AIPEL-ROUTE-001"
SOVEREIGN_BLOCKED_WARNING = (
    "Under this draft, confidential, restricted and sovereign-only requests from this application have no "
    "model: they are blocked by AI policy (AIPEL-ROUTE-001), never rerouted to another model."
)
# A neutral probe: DLP finds nothing in it, so each check's classification is the one it declares.
PROBE = "Which models may this request use?"


def switch_policy_id(application_id: str) -> str:
    return f"{SWITCH_PREFIX}{application_id}"


def switched_off(bundle: dict[str, Any], application_id: str) -> list[str]:
    """The application's granted models that its switch document in `bundle` leaves out."""
    grants = bundle["applications"].get(application_id)
    document = next(
        (p for p in bundle["policies"] if p["policy_id"] == switch_policy_id(application_id)), None
    )
    if grants is None or document is None:
        return []
    allowed = set(document["enforce"].get("allowed_models") or [])
    return sorted(m for m in grants["allowed_models"] if m not in allowed)


def _switch_document(app: Application, off: set[str]) -> PolicyDocument:
    return PolicyDocument(
        policy_id=switch_policy_id(app.id),
        title=f"Models switched off for {app.name}",
        priority=SWITCH_PRIORITY,
        match=PolicyMatch(application_any=[app.id]),
        enforce=PolicyEnforce(allowed_models=[m for m in app.allowed_models if m not in off]),
    )


def _policies(current: PolicyVersionRow, app: Application, off: set[str]) -> list[PolicyDocument]:
    """The published policies with the application's switch document replaced in place, added, or removed
    when nothing is off."""
    target = switch_policy_id(app.id)
    policies: list[PolicyDocument] = []
    placed = False
    for p in current.bundle["policies"]:
        if p["policy_id"] != target:
            policies.append(PolicyDocument.model_validate(lifecycle.document(p)))
        elif off:
            policies.append(_switch_document(app, off))
            placed = True
    if off and not placed:
        policies.append(_switch_document(app, off))
    return policies


def _probe_ip(services: Services, app: Application) -> str | None:
    """An address in one of the application's trusted networks, so each check sees the network rule pass."""
    cidrs = {n.id: n.cidr for n in services.catalog.tenant.trusted_networks}
    for network_id in app.trusted_network_ids:
        if network_id in cidrs:
            network = ipaddress.ip_network(cidrs[network_id])
            return str(next(network.hosts(), network.network_address))
    return None


def _blocked(reasons: list[DecisionReason]) -> bool:
    return any(r.rule_id == ROUTE_RULE for r in reasons)


async def _checks(
    services: Services, app: Application, before: PolicyVersionRow, after: PolicyVersionRow, tenant_id: str
) -> list[ModelSwitchCheck]:
    checks = []
    source_ip = _probe_ip(services, app)
    for classification in Classification:
        if classification.rank < app.classification_floor.rank:
            continue
        sample = PolicySample(
            application_id=app.id,
            source_ip=source_ip,
            source_mode="observed",
            identity_mode="authenticated",
            messages=[ChatMessage(role="user", content=PROBE)],
            data_classification=classification,
        )
        policy_input = lifecycle.sample_input(services, sample, tenant_id)
        old = await services.opa.simulate(before.bundle, policy_input)
        new = await services.opa.simulate(after.bundle, policy_input)
        checks.append(
            ModelSwitchCheck(
                classification=classification,
                sovereign_path=classification.requires_sovereign_path,
                allowed_models_before=sorted(old.effective.allowed_models),
                allowed_models_after=sorted(new.effective.allowed_models),
                blocked=_blocked(new.reasons),
                blocked_before=_blocked(old.reasons),
            )
        )
    return checks


async def switch_model(
    services: Services, session: AsyncSession, caller: Caller, body: ModelSwitchRequest
) -> ModelSwitchResponse:
    app = services.catalog.application(body.application_id)
    if app is None or caller.tenant_id != services.catalog.tenant.tenant.id:
        raise NotFoundError("Application not found.")
    if body.model_id not in app.allowed_models:
        raise ValidationFailedError(
            f"{body.model_id} is not granted to {app.id}.",
            code="MODEL_NOT_GRANTED",
            remediation="Only a granted model can be switched. Grants change in config/ by pull request.",
        )
    current = await latest_published(session, caller.tenant_id)
    if current is None:
        raise DependencyUnavailableError(
            "No policy version is published.",
            code="POLICY_BUNDLE_MISSING",
            remediation="Publish a policy version in AiVa Govern.",
        )
    off = set(switched_off(current.bundle, app.id)) & set(app.allowed_models)
    if (body.model_id not in off) == body.enabled:
        state = "on" if body.enabled else "off"
        raise ConflictError(
            f"{body.model_id} is already {state} for {app.id} in the published version.",
            code="MODEL_SWITCH_UNCHANGED",
        )
    if body.enabled:
        off.discard(body.model_id)
    else:
        off.add(body.model_id)
    actor = caller.actor_ref
    note = f"Model switch (ADR-0027): {body.model_id} {'on' if body.enabled else 'off'} for {app.id}."
    row = await lifecycle.create_draft(
        services,
        session,
        _policies(current, app, off),
        actor=actor,
        notes=f"{note} {body.notes}" if body.notes else note,
    )
    checks = await _checks(services, app, current, row, caller.tenant_id)
    sovereign_blocked = any(c.blocked for c in checks if c.sovereign_path)
    sovereign_blocked_before = any(c.blocked_before for c in checks if c.sovereign_path)
    detail = await lifecycle.detail(session, row, viewer_actor=actor)
    session.add(
        AuditLogRow(
            tenant_id=caller.tenant_id,
            actor_kind="user",
            actor_id=actor,
            action="policy.model_switch",
            target_type="policy_version",
            target_id=str(row.version),
            details={
                "application_id": app.id,
                "model_id": body.model_id,
                "enabled": body.enabled,
                "base_version": current.version,
                "switched_off_models": sorted(off),
                "sovereign_blocked": sovereign_blocked,
                "sovereign_blocked_before": sovereign_blocked_before,
            },
        )
    )
    return ModelSwitchResponse(
        draft=detail,
        base_version=current.version,
        application_id=app.id,
        model_id=body.model_id,
        enabled=body.enabled,
        switched_off_models=sorted(off),
        high_impact=any(c.high_impact for c in detail.changes_from_published),
        checks=checks,
        sovereign_blocked=sovereign_blocked,
        sovereign_blocked_before=sovereign_blocked_before,
        warnings=[SOVEREIGN_BLOCKED_WARNING] if sovereign_blocked else [],
    )
