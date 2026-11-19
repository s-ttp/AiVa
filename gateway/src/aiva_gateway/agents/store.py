"""Persist and read agent workflows. Stored rows hold IDs, decisions and hashes, never tool or model text."""

import hashlib
import json
from typing import Any

from sqlalchemy import ColumnElement, false, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from aiva_gateway.agents.service_incident import AGENT_ID, DEFINITION_VERSION, PHASES, Pause, Step, Workflow
from aiva_gateway.auth.principal import Caller
from aiva_gateway.contracts.agents import (
    AgentStepOut,
    AgentWorkflowOut,
    AgentWorkflowRequest,
    ModelStepOut,
    ToolCallOut,
    WorkflowBudgetOut,
)
from aiva_gateway.core.enums import (
    ChargeType,
    CimState,
    Classification,
    ExecutionPath,
    Provenance,
    Role,
    RunStatus,
)
from aiva_gateway.core.errors import ConflictError
from aiva_gateway.db.models import (
    AgentStepRow,
    AgentWorkflowRow,
    AuditLogRow,
    ExecutionRunRow,
    UsageLedgerRow,
)
from aiva_gateway.finops.scopes import configured_domain, least_trusted, visitor_domain
from aiva_gateway.finops.workflows import workflow_budget as finops_workflow_budget
from aiva_gateway.pipeline.recorder import run_provenance
from aiva_gateway.services import Services


def workflow_scope(application_id: str, workflow_id: str) -> str:
    return f"{application_id}/{workflow_id}"


def workflow_provenance(wf: Workflow) -> Provenance:
    labels = [run_provenance(m.ctx) for m in wf.model_steps]
    if any(s.tool and s.tool.executed for s in wf.steps):
        labels.append(Provenance.SYNTHETIC)  # tool output comes from synthetic systems
    return least_trusted(labels) or Provenance.SIMULATED


def can_read_workflow(caller: Caller, row: AgentWorkflowRow) -> bool:
    """Same row-level scope as run drilldown (LLD §21.8)."""
    if caller.tenant_id != row.tenant_id:
        return False
    if caller.kind == "application":
        return row.application_id == caller.application_id
    if caller.has_any(Role.PLATFORM_ADMIN, Role.TENANT_ADMIN, Role.SECURITY_AUDITOR):
        return True
    if caller.has_any(Role.DEPARTMENT_MANAGER) and row.department_id == caller.department_id:
        return True
    if caller.has_any(Role.APPLICATION_OWNER) and row.application_id in caller.owned_application_ids:
        return True
    # Console roles read their own workflows; every public visitor shares one principal (ADR-0019).
    return (
        caller.has_any(Role.TESTER, Role.PUBLIC_VISITOR)
        and row.principal_pseudonym is not None
        and (row.principal_pseudonym == caller.pseudonym)
    )


def _text_sha256(step: Step) -> str | None:
    """Hash of a model step's text, so a resumed workflow can prove its evidence is unchanged."""
    text = step.model.ctx.response_content if step.model else None
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None


def _model_approval(step: Step) -> str | None:
    """The approval a model step raised when it was held (request policy or budget)."""
    if step.model is None or step.status != "approval_required":
        return None
    return step.model.ctx.approval_id


def readable_workflows(caller: Caller) -> ColumnElement[bool]:
    """`can_read_workflow` as a query filter, so listing is scoped in the database."""
    row = AgentWorkflowRow
    if caller.kind == "application":
        return row.application_id == caller.application_id
    if caller.has_any(Role.PLATFORM_ADMIN, Role.TENANT_ADMIN, Role.SECURITY_AUDITOR):
        return true()
    clauses: list[ColumnElement[bool]] = []
    if caller.has_any(Role.DEPARTMENT_MANAGER) and caller.department_id:
        clauses.append(row.department_id == caller.department_id)
    if caller.has_any(Role.APPLICATION_OWNER) and caller.owned_application_ids:
        clauses.append(row.application_id.in_(sorted(caller.owned_application_ids)))
    if caller.has_any(Role.TESTER, Role.PUBLIC_VISITOR) and caller.pseudonym:
        clauses.append(row.principal_pseudonym == caller.pseudonym)
    return or_(*clauses) if clauses else false()


async def save_workflow(session: AsyncSession, wf: Workflow) -> None:
    """Insert a new workflow, or update a resumed one and append the steps this invocation ran."""
    identity = wf.identity
    actor_id = identity.principal_pseudonym or identity.application_id
    status = wf.status
    provenance = workflow_provenance(wf)
    row = await session.get(AgentWorkflowRow, wf.workflow_id, with_for_update=True)
    if row is None:
        session.add(
            AgentWorkflowRow(
                workflow_id=wf.workflow_id,
                tenant_id=wf.tenant_id,
                application_id=identity.application_id,
                department_id=wf.department_id,
                principal_kind=identity.principal_kind,
                principal_pseudonym=identity.principal_pseudonym,
                agent=AGENT_ID,
                definition_version=DEFINITION_VERSION,
                incident_id=wf.request.incident_id,
                status=status,
                provenance=provenance.value,
                data_designation=wf.data_designation,
                error_code=wf.error.code if wf.error else None,
                created_at=wf.created_at,
                completed_at=wf.completed_at,
            )
        )
    else:
        row.status = status
        row.provenance = (least_trusted([Provenance(row.provenance), provenance]) or provenance).value
        row.error_code = wf.error.code if wf.error else None
        row.completed_at = wf.completed_at
    await session.flush()
    for step in wf.steps:
        tool = step.tool
        run_id = step.model.ctx.run_id if step.model else step.authorized_by_run_id
        session.add(
            AgentStepRow(
                workflow_id=wf.workflow_id,
                step_index=step.index,
                kind=step.kind,
                name=step.name,
                status=step.status,
                message=step.message,
                run_id=run_id,
                tool_id=tool.tool_id if tool else None,
                tool_access=tool.access if tool else None,
                tool_egress=tool.egress if tool else None,
                arguments=dict(tool.arguments) if tool else {},
                policy_action=tool.action.value if tool else None,
                rule_ids=list(tool.rule_ids) if tool else [],
                approval_id=tool.approval_id if tool else _model_approval(step),
                executed=bool(tool and tool.executed),
                evidence_id=tool.evidence_id if tool else None,
                output_sha256=tool.output_sha256 if tool else _text_sha256(step),
                started_at=step.started_at,
                completed_at=step.completed_at,
            )
        )
        if tool is not None:
            # Every agent tool decision is audited, with the run whose policy stage made it.
            session.add(
                AuditLogRow(
                    tenant_id=wf.tenant_id,
                    actor_kind=identity.principal_kind,
                    actor_id=actor_id,
                    action=f"agent.tool.{step.status}",
                    target_type="tool",
                    target_id=tool.tool_id,
                    details={
                        "workflow_id": wf.workflow_id,
                        "run_id": run_id,
                        "policy_action": tool.action.value,
                        "rule_ids": list(tool.rule_ids),
                        "executed": tool.executed,
                        "approval_id": tool.approval_id,
                    },
                )
            )
    session.add(
        AuditLogRow(
            tenant_id=wf.tenant_id,
            actor_kind=identity.principal_kind,
            actor_id=actor_id,
            action="agent.workflow",
            target_type="agent_workflow",
            target_id=wf.workflow_id,
            details={
                "agent": AGENT_ID,
                "definition_version": DEFINITION_VERSION,
                "status": status,
                "resumed_with": wf.resumed_with,
                "run_ids": [m.ctx.run_id for m in wf.model_steps],
            },
        )
    )


async def workflow_budget(
    services: Services,
    session: AsyncSession,
    tenant_id: str,
    application_id: str,
    workflow_id: str,
    owner_pseudonym: str | None = None,
) -> WorkflowBudgetOut:
    """The workflow's FinOps budget as FinOps reads it (cap, committed, remaining, provenance), in the money
    domain its steps ran in: the gateway's configured one, or a public visitor's own (ADR-0008)."""
    domains = [configured_domain(services.settings.inference_mode)]
    if owner_pseudonym:
        domains.append(visitor_domain(owner_pseudonym))
    budget = await finops_workflow_budget(
        services.quota,
        session,
        tenant_id=tenant_id,
        application_id=application_id,
        workflow_id=workflow_id,
        domains=domains,
    )
    return WorkflowBudgetOut(
        scope_id=budget.scope_id,
        cap_micros=budget.cap_micros,
        committed_micros=budget.spent_micros,
        remaining_micros=budget.remaining_micros,
        on_exhaustion=budget.on_exhaustion,
        provenance=budget.provenance,
    )


def _step_out(step: Step, *, include_text: bool) -> AgentStepOut:
    model = None
    if step.model is not None:
        ctx = step.model.ctx
        model = ModelStepOut(
            run_id=ctx.run_id,
            trace_id=ctx.trace_id,
            status=ctx.status,
            selected_model=ctx.selected_model.id if ctx.selected_model else None,
            execution_path=ctx.path,
            cim_state=ctx.cim_state,
            effective_classification=ctx.classification.effective if ctx.classification else None,
            cost_micros=ctx.api_cost_micros,
            provenance=run_provenance(ctx),
            response=ctx.response_content if include_text else None,
            error=ctx.error,
        )
    tool = None
    if step.tool is not None:
        call = step.tool
        tool = ToolCallOut(
            tool_id=call.tool_id,
            access=call.access,
            egress=call.egress,
            arguments=call.arguments,
            policy_action=call.action,
            rule_ids=call.rule_ids,
            approval_id=call.approval_id,
            executed=call.executed,
            evidence_id=call.evidence_id,
            output_sha256=call.output_sha256,
            output=call.output if include_text else None,
        )
    return AgentStepOut(
        index=step.index,
        kind=step.kind,
        name=step.name,
        status=step.status,
        message=step.message,
        authorized_by_run_id=step.authorized_by_run_id,
        model=model,
        tool=tool,
        started_at=step.started_at,
        completed_at=step.completed_at,
    )


# --- Pauses ---------------------------------------------------------------------------------------------


def _pause_key(tenant_id: str, workflow_id: str) -> str:
    return f"aiva:agent:pause:{tenant_id}:{workflow_id}"


async def save_pause(services: Services, wf: Workflow) -> None:
    """Keep a paused workflow resumable until its approval expires (Redis only; lost means not resumable)."""
    if wf.pause is None:
        return
    payload = {"request": wf.request.model_dump(mode="json", exclude={"test_profile"}), **wf.pause.to_json()}
    await services.redis.set(
        _pause_key(wf.tenant_id, wf.workflow_id),
        json.dumps(payload),
        ex=services.settings.approval_ttl_seconds,
    )


async def load_pause(
    services: Services, tenant_id: str, workflow_id: str
) -> tuple[AgentWorkflowRequest, Pause] | None:
    raw = await services.redis.get(_pause_key(tenant_id, workflow_id))
    if raw is None:
        return None
    data = json.loads(raw)
    return AgentWorkflowRequest.model_validate(data["request"]), Pause.from_json(data)


async def verify_pause(session: AsyncSession, workflow_id: str, incident_id: str, pause: Pause) -> None:
    """The paused step must be resubmitted byte-identical (the approval is bound to its request hash), so
    every evidence item kept in Redis must match the output hash stored in Postgres when it was produced.
    A recorded tool call's arguments must match the step row that raised its approval."""
    phase_outputs = {p.name: p.output_evidence for p in PHASES if p.output_evidence}
    expected: dict[str, str | None] = {}
    rows = list(
        await session.scalars(
            select(AgentStepRow)
            .where(AgentStepRow.workflow_id == workflow_id)
            .order_by(AgentStepRow.step_index)
        )
    )
    if pause.tool_index is not None:
        # Postgres is the source of truth for the recorded call; the Redis copy must match it.
        recorded = [
            row.arguments
            for row in rows
            if row.kind == "tool"
            and row.status == "approval_required"
            and row.approval_id == pause.approval_id
        ]
        if recorded != [pause.tool_arguments]:
            raise ConflictError(
                "The paused tool call no longer matches what the workflow recorded.",
                code="WORKFLOW_PAUSE_INVALID",
                remediation="Start a new workflow.",
            )
    for row in rows:
        if row.kind == "tool" and row.evidence_id:
            expected[row.evidence_id] = row.output_sha256
        elif row.kind == "model" and row.name in phase_outputs and row.output_sha256:
            expected[f"{incident_id}/{phase_outputs[row.name]}"] = row.output_sha256
    for item in (*pause.phase_evidence, *pause.evidence):
        digest = hashlib.sha256(item.text.encode("utf-8")).hexdigest()
        if expected.get(item.id) != digest:
            raise ConflictError(
                "The paused step's evidence no longer matches what the workflow recorded.",
                code="WORKFLOW_PAUSE_INVALID",
                remediation="Start a new workflow.",
            )


async def clear_pause(services: Services, tenant_id: str, workflow_id: str) -> None:
    await services.redis.delete(_pause_key(tenant_id, workflow_id))


# --- Read models ----------------------------------------------------------------------------------------


def workflow_out(wf: Workflow, budget: WorkflowBudgetOut) -> AgentWorkflowOut:
    """The POST response: includes tool output and model text for the caller who ran it."""
    return AgentWorkflowOut(
        workflow_id=wf.workflow_id,
        agent=AGENT_ID,
        definition_version=DEFINITION_VERSION,
        tenant_id=wf.tenant_id,
        application_id=wf.identity.application_id,
        incident_id=wf.request.incident_id,
        status=wf.status,
        steps=[_step_out(s, include_text=True) for s in wf.steps],
        pending_approval_ids=[wf.pause.approval_id] if wf.pause else [],
        human_review_tool_ids=[s.name for s in wf.steps if s.status == "human_review"],
        report=wf.report,
        budget=budget,
        provenance=workflow_provenance(wf),
        data_designation="actual" if wf.data_designation == "actual" else "synthetic",
        created_at=wf.created_at,
        completed_at=wf.completed_at,
        error=wf.error,
    )


async def resumed_workflow_out(
    services: Services, session: AsyncSession, row: AgentWorkflowRow, wf: Workflow
) -> AgentWorkflowOut:
    """A resumed workflow: stored steps without text, then this invocation's steps with their text."""
    stored = await load_workflow_out(services, session, row)
    fresh = {s.index: _step_out(s, include_text=True) for s in wf.steps}
    return stored.model_copy(
        update={
            "steps": [fresh.get(s.index, s) for s in stored.steps],
            "report": wf.report,
            "error": wf.error,
        }
    )


async def load_workflow_out(
    services: Services, session: AsyncSession, row: AgentWorkflowRow
) -> AgentWorkflowOut:
    """A stored workflow: decisions, routes, costs and hashes; no tool output or model text."""
    steps = (
        (
            await session.execute(
                select(AgentStepRow)
                .where(AgentStepRow.workflow_id == row.workflow_id)
                .order_by(AgentStepRow.step_index)
            )
        )
        .scalars()
        .all()
    )
    run_ids = [s.run_id for s in steps if s.kind == "model" and s.run_id]
    runs = {
        r.run_id: r
        for r in (
            await session.execute(select(ExecutionRunRow).where(ExecutionRunRow.run_id.in_(run_ids)))
        ).scalars()
    }
    costs = await _api_costs(session, run_ids)
    out_steps = []
    for s in steps:
        model = None
        run = runs.get(s.run_id) if s.kind == "model" and s.run_id else None
        if run is not None:
            model = ModelStepOut(
                run_id=run.run_id,
                trace_id=run.trace_id,
                status=RunStatus(run.status),
                selected_model=run.selected_model,
                execution_path=ExecutionPath(run.execution_path),
                cim_state=CimState(run.cim_state),
                effective_classification=Classification(run.effective_classification),
                cost_micros=costs.get(run.run_id, 0),
                provenance=Provenance(run.provenance),
                error=None,
            )
        tool = None
        if s.kind == "tool" and s.tool_id:
            tool = ToolCallOut(
                tool_id=s.tool_id,
                access=_access(s.tool_access),
                egress=_egress(s.tool_egress),
                arguments=dict(s.arguments),
                policy_action=s.policy_action,
                rule_ids=list(s.rule_ids),
                approval_id=s.approval_id,
                executed=s.executed,
                evidence_id=s.evidence_id,
                output_sha256=s.output_sha256,
            )
        out_steps.append(
            AgentStepOut(
                index=s.step_index,
                kind=s.kind,
                name=s.name,
                status=s.status,
                message=s.message,
                authorized_by_run_id=s.run_id if s.kind == "tool" else None,
                model=model,
                tool=tool,
                started_at=s.started_at,
                completed_at=s.completed_at,
            )
        )
    budget = await workflow_budget(
        services, session, row.tenant_id, row.application_id, row.workflow_id, row.principal_pseudonym
    )
    return AgentWorkflowOut(
        workflow_id=row.workflow_id,
        agent=row.agent,
        definition_version=row.definition_version,
        tenant_id=row.tenant_id,
        application_id=row.application_id,
        incident_id=row.incident_id,
        status=row.status,
        steps=out_steps,
        pending_approval_ids=[steps[-1].approval_id]
        if row.status == "approval_required" and steps and steps[-1].approval_id
        else [],
        human_review_tool_ids=[s.name for s in steps if s.status == "human_review"],
        budget=budget,
        provenance=Provenance(row.provenance),
        data_designation="actual" if row.data_designation == "actual" else "synthetic",
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


async def _api_costs(session: AsyncSession, run_ids: list[str]) -> dict[str, int]:
    """Provider-billable charges per run from the append-only ledger (GPU estimates excluded)."""
    rows = await session.execute(
        select(UsageLedgerRow.run_id, func.sum(UsageLedgerRow.amount_micros))
        .where(
            UsageLedgerRow.run_id.in_(run_ids),
            UsageLedgerRow.charge_type != ChargeType.GPU_ESTIMATE.value,
        )
        .group_by(UsageLedgerRow.run_id)
    )
    return {run_id: int(total or 0) for run_id, total in rows.all()}


def _access(value: str | None) -> Any:
    return value if value in ("read", "write") else None


def _egress(value: str | None) -> Any:
    return value if value in ("internal", "external") else None
