"""Approval Queue (LLD §7, §22.5; ADR-0012).

A run whose request or tool decision is `approval_required`, or whose budget reservation is denied with
`on_exhaustion: approval_required`, becomes an approval request holding policy facts only (the OPA
input: identity, source, hints, classification levels, DLP finding types), never prompt or evidence
text. Approving re-evaluates those facts against the current policy version: if that version denies,
the request is closed instead. The caller resumes by resubmitting the identical request with
`approval_id`; the approval is single-use, bound to the request hash, application and requester, and
expires. Policy is evaluated again on resume, and an approval never overrides a deny. A budget approval
records the denied envelope (`approved_amount_micros`); how much it unlocks is FinOps' decision.

A tool approval whose call was recorded with its arguments (`arguments_sha256`) can instead be executed
without a new run: `authorize_recorded_tool_call` re-evaluates the recorded tool-stage facts against the
current policy, with the current caller's identity and source, and consumes the approval only on allow.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aiva_gateway.auth.principal import Caller, RunIdentity
from aiva_gateway.catalog import Catalog
from aiva_gateway.contracts.govern import ApprovalDecisionResponse, ApprovalRequestOut
from aiva_gateway.contracts.policy import (
    ApprovalGrant,
    DecisionReason,
    PolicyApproval,
    PolicyDecision,
    PolicyIdentity,
    PolicyInput,
    PolicySource,
)
from aiva_gateway.core.enums import Classification, PolicyAction, PolicyStage, Role, RunStatus
from aiva_gateway.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from aiva_gateway.db.models import ApprovalRequestRow, AuditLogRow, PolicyEventRow
from aiva_gateway.govern.policy_input import tool_arguments_sha256
from aiva_gateway.pipeline.context import RunContext
from aiva_gateway.pipeline.interfaces import PolicyEngine

_LIVE = ("pending", "approved")


def approval_rows(ctx: RunContext) -> list[ApprovalRequestRow]:
    """Approval requests for every request or tool decision of the run that required approval."""
    rows = []
    for event in ctx.policy_events:
        if event.approval_id is None or event.policy_input is None or event.approval_expires_at is None:
            continue
        reasons = event.decision.reasons
        tool = event.policy_input.tool
        rows.append(
            ApprovalRequestRow(
                id=event.approval_id,
                run_id=ctx.run_id,
                tenant_id=ctx.tenant_id,
                application_id=ctx.identity.application_id,
                kind="tool" if event.stage is PolicyStage.TOOL else "policy",
                tool_id=event.tool_id,
                arguments_sha256=tool.arguments_sha256 if tool else None,
                rule_ids=[r.rule_id for r in reasons],
                reasons=[r.model_dump(mode="json") for r in reasons],
                status="pending",
                request_hash=ctx.request_hash,
                principal_kind=ctx.identity.principal_kind,
                principal_pseudonym=ctx.identity.principal_pseudonym,
                policy_input=event.policy_input.model_dump(mode="json"),
                policy_version_at_request=event.decision.policy_version,
                bundle_hash_at_request=event.decision.bundle_hash,
                requested_at=ctx.created_at,
                expires_at=event.approval_expires_at,
            )
        )
    budget = ctx.budget_approval
    decision = ctx.request_decision
    if budget is not None and budget.policy_input is not None and decision is not None:
        reason = DecisionReason(
            rule_id=budget.rule_id,
            message=budget.message,
            remediation="A budget owner must approve this request in the AiVa Govern approval queue.",
        )
        rows.append(
            ApprovalRequestRow(
                id=budget.approval_id,
                run_id=ctx.run_id,
                tenant_id=ctx.tenant_id,
                application_id=ctx.identity.application_id,
                kind="budget",
                rule_ids=[budget.rule_id],
                reasons=[reason.model_dump(mode="json")],
                status="pending",
                request_hash=ctx.request_hash,
                principal_kind=ctx.identity.principal_kind,
                principal_pseudonym=ctx.identity.principal_pseudonym,
                policy_input=budget.policy_input.model_dump(mode="json"),
                policy_version_at_request=decision.policy_version,
                bundle_hash_at_request=decision.bundle_hash,
                approved_amount_micros=budget.amount_micros,
                requested_at=ctx.created_at,
                expires_at=budget.expires_at,
            )
        )
    return rows


def _status(row: ApprovalRequestRow, now: datetime) -> str:
    if row.status in _LIVE and row.expires_at <= now:
        return "expired"
    return row.status


async def expire_stale(session: AsyncSession, tenant_id: str) -> None:
    await session.execute(
        update(ApprovalRequestRow)
        .where(
            ApprovalRequestRow.tenant_id == tenant_id,
            ApprovalRequestRow.status.in_(_LIVE),
            ApprovalRequestRow.expires_at <= datetime.now(UTC),
        )
        .values(status="expired")
    )


def _audit(
    row: ApprovalRequestRow, *, actor_kind: str, actor_id: str, action: str, **details: object
) -> AuditLogRow:
    return AuditLogRow(
        tenant_id=row.tenant_id,
        actor_kind=actor_kind,
        actor_id=actor_id,
        action=action,
        target_type="approval_request",
        target_id=row.id,
        details={"run_id": row.run_id, "rule_ids": row.rule_ids, **details},
    )


# --- Resume ------------------------------------------------------------------------------------


async def claim_approval(
    session: AsyncSession,
    *,
    approval_id: str,
    tenant_id: str,
    identity: RunIdentity,
    request_hash: str,
    run_id: str,
) -> PolicyApproval:
    """Verify and consume an approval for a resubmitted request. Raises when it cannot be used."""
    row = await session.get(ApprovalRequestRow, approval_id, with_for_update=True)
    if (
        row is None
        or row.tenant_id != tenant_id
        or row.application_id != identity.application_id
        or row.principal_kind != identity.principal_kind
        or row.principal_pseudonym != identity.principal_pseudonym
    ):
        raise NotFoundError("Approval not found.", code="APPROVAL_NOT_FOUND")
    now = datetime.now(UTC)
    status = _status(row, now)
    if status != "approved":
        raise ConflictError(
            f"The approval cannot be used: it is {status}.",
            code="APPROVAL_NOT_USABLE",
            remediation="Wait for an approver, or submit the request again to create a new approval request.",
            details={"status": status},
        )
    if row.request_hash != request_hash:
        raise ConflictError(
            "The request differs from the approved request.",
            code="APPROVAL_REQUEST_MISMATCH",
            remediation="Resubmit exactly the request that was approved.",
        )
    row.status, row.consumed_by_run_id, row.consumed_at = "consumed", run_id, now
    session.add(
        _audit(
            row,
            actor_kind=identity.principal_kind,
            actor_id=identity.principal_pseudonym or identity.application_id,
            action="approval.consume",
            consumed_by_run_id=run_id,
        )
    )
    return PolicyApproval(
        approval_id=row.id, grants=[ApprovalGrant.model_validate(g) for g in row.approved_grants]
    )


def releasable(ctx: RunContext) -> bool:
    """A claimed approval is returned only when the run failed on a retryable dependency error."""
    return ctx.status is RunStatus.FAILED and ctx.error is not None and ctx.error.retryable


async def release_approval(session: AsyncSession, approval: PolicyApproval, *, run_id: str) -> None:
    """Return a claimed approval the run did not use. Only the claiming run can release it; audited."""
    released = await session.execute(
        update(ApprovalRequestRow)
        .where(
            ApprovalRequestRow.id == approval.approval_id,
            ApprovalRequestRow.status == "consumed",
            ApprovalRequestRow.consumed_by_run_id == run_id,
        )
        .values(status="approved", consumed_by_run_id=None, consumed_at=None)
        .returning(ApprovalRequestRow.tenant_id, ApprovalRequestRow.run_id, ApprovalRequestRow.rule_ids)
    )
    for tenant_id, approval_run_id, rule_ids in released.all():
        session.add(
            AuditLogRow(
                tenant_id=tenant_id,
                actor_kind="system",
                actor_id="aiva-gateway",
                action="approval.release",
                target_type="approval_request",
                target_id=approval.approval_id,
                details={"run_id": approval_run_id, "rule_ids": rule_ids, "released_by_run_id": run_id},
            )
        )


# --- Recorded tool calls -----------------------------------------------------------------------


@dataclass(frozen=True)
class RecordedToolDecision:
    """The current policy's decision on a recorded tool call, and whether its approval was consumed."""

    decision: PolicyDecision
    approval_id: str
    run_id: str  # the run the call was recorded on; the decision is stored as its next policy event
    consumed: bool


async def authorize_recorded_tool_call(
    session: AsyncSession,
    policy_engine: PolicyEngine,
    catalog: Catalog,
    *,
    approval_id: str,
    tenant_id: str,
    identity: RunIdentity,
    tool_id: str,
    arguments: Mapping[str, Any],
) -> RecordedToolDecision:
    """Re-authorize the exact tool call a tool approval was recorded for, without a new run.

    The approval must be approved, unexpired, and bound to the same tenant, application, principal, tool
    and arguments. The recorded tool-stage facts (classification, DLP and evidence-injection facts, tool)
    are evaluated against the current policy with the caller's current identity and source, so a call
    from an untrusted network or under a newer, stricter policy is refused. The approval is consumed only
    on `allow`; the caller executes the tool only then. OPA unavailable raises before any state change.
    The caller owns the transaction: commit it before executing the tool.
    """
    row = await session.get(ApprovalRequestRow, approval_id, with_for_update=True)
    if (
        row is None
        or row.tenant_id != tenant_id
        or row.application_id != identity.application_id
        or row.principal_kind != identity.principal_kind
        or row.principal_pseudonym != identity.principal_pseudonym
    ):
        raise NotFoundError("Approval not found.", code="APPROVAL_NOT_FOUND")
    if (
        row.kind != "tool"
        or row.tool_id != tool_id
        or row.arguments_sha256 is None
        or row.arguments_sha256 != tool_arguments_sha256(arguments)
    ):
        raise ConflictError(
            "The tool call differs from the approved call.",
            code="APPROVAL_REQUEST_MISMATCH",
            remediation="Execute exactly the recorded tool call, with its original arguments.",
        )
    now = datetime.now(UTC)
    status = _status(row, now)
    if status != "approved":
        raise ConflictError(
            f"The approval cannot be used: it is {status}.",
            code="APPROVAL_NOT_USABLE",
            remediation="Wait for an approver, or run the step again to create a new approval request.",
            details={"status": status},
        )
    recorded = PolicyInput.model_validate(row.policy_input)
    facts = recorded.model_copy(
        update={
            "identity": PolicyIdentity(
                kind=identity.principal_kind,
                application_id=identity.application_id,
                identity_mode=identity.identity_mode,
                roles=list(identity.roles),
                department_id=catalog.department_of(identity.application_id),
            ),
            "source": PolicySource(ip=identity.source_ip, mode=identity.source_mode),
            "approvals": [
                PolicyApproval(
                    approval_id=row.id,
                    grants=[ApprovalGrant.model_validate(g) for g in row.approved_grants],
                )
            ],
        }
    )
    decision = await policy_engine.evaluate(facts)  # fails closed: raises before any state change
    consumed = decision.action == PolicyAction.ALLOW
    sequence = await session.scalar(
        select(func.coalesce(func.max(PolicyEventRow.sequence), -1) + 1).where(
            PolicyEventRow.run_id == row.run_id
        )
    )
    session.add(
        PolicyEventRow(
            run_id=row.run_id,
            sequence=int(sequence or 0),
            stage=PolicyStage.TOOL.value,
            egress_model_id=None,
            tool_id=tool_id,
            action=decision.action.value,
            policy_version=decision.policy_version,
            bundle_hash=decision.bundle_hash,
            matched_rule_ids=decision.matched_rule_ids,
            reasons=[r.model_dump(mode="json") for r in decision.reasons],
            effective=decision.effective.model_dump(mode="json"),
            declared_classification=facts.classification.declared.value
            if facts.classification.declared
            else None,
            dlp_classification=facts.classification.dlp.value if facts.classification.dlp else None,
            effective_classification=facts.classification.effective.value,
            matched_network_id=decision.matched_network_id,
            reservation_id=None,
        )
    )
    if consumed:
        row.status, row.consumed_by_run_id, row.consumed_at = "consumed", row.run_id, now
    session.add(
        _audit(
            row,
            actor_kind=identity.principal_kind,
            actor_id=identity.principal_pseudonym or identity.application_id,
            action="approval.consume" if consumed else "approval.recheck_refused",
            tool_id=tool_id,
            recorded_call=True,
            policy_action=decision.action.value,
            policy_version=decision.policy_version,
            bundle_hash=decision.bundle_hash,
            current_rule_ids=[r.rule_id for r in decision.reasons],
        )
    )
    await session.flush()
    return RecordedToolDecision(decision=decision, approval_id=row.id, run_id=row.run_id, consumed=consumed)


# --- Access ------------------------------------------------------------------------------------


def can_read(caller: Caller, row: ApprovalRequestRow, catalog: Catalog) -> bool:
    if caller.tenant_id != row.tenant_id:
        return False
    if caller.kind == "application":
        return row.principal_kind == "application" and caller.application_id == row.application_id
    if caller.pseudonym is not None and caller.pseudonym == row.principal_pseudonym:
        return True
    if caller.has_any(Role.PLATFORM_ADMIN, Role.TENANT_ADMIN, Role.SECURITY_AUDITOR):
        return True
    if caller.has_any(Role.APPLICATION_OWNER) and row.application_id in caller.owned_application_ids:
        return True
    if row.kind == "budget" and caller.has_any(Role.FINANCE_ANALYST):
        return True
    return caller.has_any(Role.DEPARTMENT_MANAGER) and (
        catalog.department_of(row.application_id) == caller.department_id
    )


def can_decide(caller: Caller, row: ApprovalRequestRow) -> bool:
    """Admins decide everything; finance analysts decide budget approvals; application owners decide
    policy and tool approvals for their own applications."""
    if caller.kind != "user" or caller.tenant_id != row.tenant_id:
        return False
    if caller.has_any(Role.PLATFORM_ADMIN, Role.TENANT_ADMIN):
        return True
    if row.kind == "budget":
        return caller.has_any(Role.FINANCE_ANALYST)
    return caller.has_any(Role.APPLICATION_OWNER) and row.application_id in caller.owned_application_ids


def requested_by(caller: Caller, row: ApprovalRequestRow) -> bool:
    """Whether the caller made the request (an application credential, or the same user)."""
    if caller.tenant_id != row.tenant_id:
        return False
    if caller.kind == "application":
        return row.principal_kind == "application" and caller.application_id == row.application_id
    return caller.pseudonym is not None and caller.pseudonym == row.principal_pseudonym


def viewer_can_decide(caller: Caller, row: ApprovalRequestRow) -> bool:
    """The same checks `decide` enforces, so the UI can disable actions the gateway would refuse."""
    return (
        can_decide(caller, row)
        and not requested_by(caller, row)
        and _status(row, datetime.now(UTC)) == "pending"
    )


# --- Decide ------------------------------------------------------------------------------------


async def decide(
    policy_engine: PolicyEngine,
    session: AsyncSession,
    row: ApprovalRequestRow,
    caller: Caller,
    *,
    approve: bool,
    note: str,
) -> ApprovalDecisionResponse:
    if not can_decide(caller, row):
        raise PermissionDeniedError(
            "You cannot decide approvals for this application.",
            remediation="Ask a tenant administrator or the application owner.",
        )
    if requested_by(caller, row):
        raise PermissionDeniedError("Approvers cannot decide their own requests.")
    now = datetime.now(UTC)
    status = _status(row, now)
    if status != "pending":
        if status == "expired":
            row.status = "expired"
        raise ConflictError(f"The approval request is {status}.", code="APPROVAL_ALREADY_DECIDED")
    actor = caller.actor_ref
    row.decided_at, row.decided_by, row.decision_note = now, actor, note
    if not approve:
        row.status = "denied"
        session.add(_audit(row, actor_kind="user", actor_id=actor, action="approval.deny"))
        return ApprovalDecisionResponse(approval=to_out(row, viewer=caller), outcome="denied")

    stored = PolicyInput.model_validate(row.policy_input)
    decision = await policy_engine.evaluate(stored)  # fails closed: no state change if OPA is down
    row.policy_version_at_decision, row.bundle_hash_at_decision = (
        decision.policy_version,
        decision.bundle_hash,
    )
    outcome: Literal["approved", "closed_by_policy"]
    if decision.action == PolicyAction.DENY:
        row.status, outcome = "denied", "closed_by_policy"
    else:
        row.status, outcome = "approved", "approved"
        grants = _grants(stored, decision, row.tool_id)
        if row.kind == "budget":
            grants.extend(ApprovalGrant(rule_id=rule_id) for rule_id in row.rule_ids)
        row.approved_grants = [g.model_dump(mode="json") for g in grants]
    session.add(
        _audit(
            row,
            actor_kind="user",
            actor_id=actor,
            action=f"approval.{outcome}",
            policy_version=decision.policy_version,
            bundle_hash=decision.bundle_hash,
            current_rule_ids=[r.rule_id for r in decision.reasons],
        )
    )
    return ApprovalDecisionResponse(
        approval=to_out(row, viewer=caller), outcome=outcome, re_evaluation=decision
    )


def _grants(stored: PolicyInput, decision: PolicyDecision, tool_id: str | None) -> list[ApprovalGrant]:
    """Grants carried from an approval the run already consumed, plus the rules approved now."""
    grants = [g for approval in stored.approvals for g in approval.grants]
    if decision.action == PolicyAction.APPROVAL_REQUIRED:
        grants.extend(ApprovalGrant(rule_id=r.rule_id, tool_id=tool_id) for r in decision.reasons)
    unique: dict[tuple[str, str | None], ApprovalGrant] = {(g.rule_id, g.tool_id): g for g in grants}
    return list(unique.values())


def to_out(
    row: ApprovalRequestRow, *, matched_network_id: str | None = None, viewer: Caller | None = None
) -> ApprovalRequestOut:
    facts = PolicyInput.model_validate(row.policy_input) if row.policy_input else None
    effective: Classification | None = facts.classification.effective if facts else None
    return ApprovalRequestOut(
        approval_id=row.id,
        kind=row.kind if row.kind in ("tool", "budget") else "policy",
        status=_status(row, datetime.now(UTC)),
        run_id=row.run_id,
        application_id=row.application_id,
        tool_id=row.tool_id,
        arguments_sha256=row.arguments_sha256,
        rule_ids=row.rule_ids,
        reasons=[DecisionReason.model_validate(r) for r in row.reasons],
        carried_grants=[g for a in facts.approvals for g in a.grants] if facts else [],
        effective_classification=effective,
        task=facts.request.task if facts else None,
        scenario_id=facts.request.scenario_id if facts else None,
        matched_network_id=matched_network_id,
        identity_mode=facts.identity.identity_mode.value if facts else None,
        source_mode=facts.source.mode.value if facts else None,
        requested_by=row.principal_pseudonym,
        requested_at=row.requested_at,
        expires_at=row.expires_at,
        policy_version_at_request=row.policy_version_at_request,
        decided_at=row.decided_at,
        decided_by=row.decided_by,
        decision_note=row.decision_note,
        approved_grants=[ApprovalGrant.model_validate(g) for g in row.approved_grants],
        policy_version_at_decision=row.policy_version_at_decision,
        consumed_by_run_id=row.consumed_by_run_id,
        approved_amount_micros=row.approved_amount_micros,
        requested_by_viewer=viewer is not None and requested_by(viewer, row),
        viewer_can_decide=viewer is not None and viewer_can_decide(viewer, row),
    )
