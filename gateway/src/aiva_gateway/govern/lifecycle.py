"""Policy lifecycle: draft, simulate, publish and roll back (LLD §22.6, ADR-0005, ADR-0012).

- A draft is a complete bundle (policies + application grants + models + tools + networks) stored with
  status `draft`. Publishing it loads exactly the bundle that was simulated, so its hash is unchanged.
- Rollback re-publishes an archived version as-is: decisions then report that version and its original
  bundle hash ("re-pin a hash").
- A high-impact draft (see `impact.py`) must be published by a different administrator than its author.
- Every action writes an audit record. OPA failures fail closed: nothing is published.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from aiva_gateway.auth.ingress import is_valid_ip
from aiva_gateway.auth.principal import RunIdentity
from aiva_gateway.catalog.loader import policy_reference_errors
from aiva_gateway.catalog.models import PolicyDocument
from aiva_gateway.contracts.chat import ChatRequest, RoutingOptions
from aiva_gateway.contracts.policy import (
    PolicyChange,
    PolicyInput,
    PolicyPublishResponse,
    PolicySample,
    PolicyTestRequest,
    PolicyTestResponse,
    PolicyTestResult,
    PolicyVersionDetail,
    PolicyVersionSummary,
)
from aiva_gateway.core.enums import IdentityMode, SourceMode
from aiva_gateway.core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from aiva_gateway.db.models import AuditLogRow, PolicyVersionRow
from aiva_gateway.govern.bundle import build_bundle
from aiva_gateway.govern.impact import bundle_changes
from aiva_gateway.govern.policy_input import build_policy_input
from aiva_gateway.govern.publisher import latest_published
from aiva_gateway.pipeline.orchestrator import Orchestrator
from aiva_gateway.services import Services


def summary(row: PolicyVersionRow) -> PolicyVersionSummary:
    return PolicyVersionSummary(
        version=row.version,
        status=row.status,
        bundle_hash=row.bundle_hash,
        created_at=row.created_at.isoformat(),
        created_by=row.created_by,
        published_at=row.published_at.isoformat() if row.published_at else None,
        published_by=row.published_by,
        notes=row.notes,
    )


async def _lock(session: AsyncSession, tenant_id: str) -> None:
    """Serialise lifecycle changes per tenant for the rest of the transaction."""
    await session.execute(
        text("select pg_advisory_xact_lock(hashtext(:key))"), {"key": f"policy:{tenant_id}"}
    )


async def get_version(session: AsyncSession, tenant_id: str, version: int) -> PolicyVersionRow:
    row = (
        await session.execute(
            select(PolicyVersionRow).where(
                PolicyVersionRow.tenant_id == tenant_id, PolicyVersionRow.version == version
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"Policy version {version} not found.")
    return row


def _audit(tenant_id: str, actor: str, action: str, target_id: str, **details: Any) -> AuditLogRow:
    return AuditLogRow(
        tenant_id=tenant_id,
        actor_kind="user",
        actor_id=actor,
        action=action,
        target_type="policy_version",
        target_id=target_id,
        details=details,
    )


async def detail(
    session: AsyncSession, row: PolicyVersionRow, *, viewer_actor: str | None = None
) -> PolicyVersionDetail:
    current = await latest_published(session, row.tenant_id)
    changes = [] if current is None or current.id == row.id else bundle_changes(current.bundle, row.bundle)
    return PolicyVersionDetail(
        **summary(row).model_dump(),
        policies=[PolicyDocument.model_validate(document(p)) for p in row.bundle["policies"]],
        published_version=current.version if current else None,
        changes_from_published=changes,
        authored_by_viewer=viewer_actor is not None and row.created_by == viewer_actor,
    )


def document(policy: dict[str, Any]) -> dict[str, Any]:
    return {k: policy[k] for k in ("policy_id", "title", "priority", "match", "enforce")}


async def create_draft(
    services: Services,
    session: AsyncSession,
    policies: list[PolicyDocument],
    *,
    actor: str,
    notes: str | None,
) -> PolicyVersionRow:
    errors = policy_reference_errors(services.catalog, policies)
    if errors:
        raise ValidationFailedError("The draft has invalid references.", details={"errors": errors})
    tenant_id = services.catalog.tenant.tenant.id
    await _lock(session, tenant_id)
    highest = await session.scalar(
        select(func.max(PolicyVersionRow.version)).where(PolicyVersionRow.tenant_id == tenant_id)
    )
    version = (highest or 0) + 1
    bundle = build_bundle(
        services.catalog,
        policies,
        version=str(version),
        allow_simulated_ingress=services.settings.allow_simulated_ingress,
    )
    row = PolicyVersionRow(
        tenant_id=tenant_id,
        version=version,
        status="draft",
        bundle=bundle,
        bundle_hash=bundle["meta"]["bundle_hash"],
        notes=notes,
        created_by=actor,
        created_at=datetime.now(UTC),
    )
    session.add(row)
    session.add(
        _audit(
            tenant_id,
            actor,
            "policy.draft",
            str(version),
            bundle_hash=row.bundle_hash,
            policy_ids=[p.policy_id for p in policies],
            notes=notes,
        )
    )
    await session.flush()
    return row


async def simulate(
    services: Services, session: AsyncSession, body: PolicyTestRequest, *, tenant_id: str, actor: str
) -> PolicyTestResponse:
    if body.version is None:
        row = await latest_published(session, tenant_id)
        if row is None:
            raise NotFoundError("No published policy version.")
    else:
        row = await get_version(session, tenant_id, body.version)
    results = []
    for case in body.cases:
        policy_input = (
            case.input if case.input is not None else sample_input(services, case.sample, tenant_id)
        )
        decision = await services.opa.simulate(row.bundle, policy_input)
        passed = None if case.expected_action is None else decision.action == case.expected_action
        results.append(
            PolicyTestResult(
                name=case.name,
                input=policy_input,
                decision=decision,
                expected_action=case.expected_action,
                passed=passed,
            )
        )
    passed_count = sum(1 for r in results if r.passed is True)
    failed_count = sum(1 for r in results if r.passed is False)
    session.add(
        _audit(
            tenant_id,
            actor,
            "policy.simulate",
            str(row.version),
            bundle_hash=row.bundle_hash,
            cases=len(results),
            passed=passed_count,
            failed=failed_count,
        )
    )
    return PolicyTestResponse(
        version=row.version,
        status=row.status,
        bundle_hash=row.bundle_hash,
        results=results,
        passed=passed_count,
        failed=failed_count,
    )


def sample_input(services: Services, sample: PolicySample | None, tenant_id: str) -> PolicyInput:
    """Turn a sample request into the policy input the gateway would send, running real DLP on it."""
    assert sample is not None
    if sample.source_ip is not None and not is_valid_ip(sample.source_ip):
        raise ValidationFailedError("sample.source_ip is not a valid IP address.")
    simulated = sample.identity_mode == "simulated"
    identity = RunIdentity(
        application_id=sample.application_id,
        identity_mode=IdentityMode(sample.identity_mode),
        source_ip=sample.source_ip,
        source_mode=SourceMode(sample.source_mode),
        principal_kind="user" if simulated else "application",
        principal_pseudonym=None,
        roles=tuple(sorted(sample.roles)),
    )
    request = ChatRequest(
        messages=sample.messages,
        evidence=sample.evidence,
        data_classification=sample.data_classification,
        task=sample.task,
        scenario_id=sample.scenario_id,
        routing=RoutingOptions(mode=sample.routing_mode, tags=sample.tags),
    )
    orchestrator = Orchestrator(services)
    ctx = orchestrator.new_context(request, identity, tenant_id)
    orchestrator.classify(ctx)
    built = build_policy_input(ctx, sample.stage, egress=sample.egress, tool=sample.tool)
    return built.model_copy(update={"approvals": sample.approvals})


async def publish(
    services: Services, session: AsyncSession, version: int, *, actor: str, approval_note: str, tenant_id: str
) -> PolicyPublishResponse:
    await _lock(session, tenant_id)
    row = await get_version(session, tenant_id, version)
    if row.status == "published":
        raise ConflictError(f"Policy version {version} is already published.", code="ALREADY_PUBLISHED")
    current = await latest_published(session, tenant_id)
    changes: list[PolicyChange] = bundle_changes(current.bundle if current else None, row.bundle)
    high_impact = any(c.high_impact for c in changes)
    kind = "publish" if row.status == "draft" else "rollback"
    if kind == "publish" and high_impact and row.created_by == actor:
        raise PermissionDeniedError(
            "A high-impact change must be published by a different administrator than its author.",
            code="SEPARATION_OF_DUTIES",
            remediation="Ask another administrator to review the changes and publish this version.",
            details={"high_impact": [c.target_id for c in changes if c.high_impact]},
        )
    now = datetime.now(UTC)
    if current is not None:
        current.status = "archived"
    row.status, row.published_by, row.published_at = "published", actor, now
    session.add(
        _audit(
            tenant_id,
            actor,
            f"policy.{kind}",
            str(version),
            bundle_hash=row.bundle_hash,
            previous_version=current.version if current else None,
            previous_bundle_hash=current.bundle_hash if current else None,
            approval_note=approval_note,
            high_impact=high_impact,
            changes=[c.model_dump(mode="json") for c in changes],
        )
    )
    await session.flush()
    # Load into OPA last: if it fails, the transaction rolls back and the published version is unchanged.
    await services.opa.publish_bundle(row.bundle)
    return PolicyPublishResponse(
        version=row.version,
        bundle_hash=row.bundle_hash,
        published_at=now.isoformat(),
        kind=kind,
        previous_version=current.version if current else None,
        previous_bundle_hash=current.bundle_hash if current else None,
        changes=changes,
        details={"high_impact": high_impact, "approval_note": approval_note},
    )
