"""Run drilldown. Row-level scope is enforced in the query layer (LLD §21.8); unreadable runs return 404."""

from fastapi import APIRouter
from sqlalchemy import select

from aiva_gateway.api.deps import CallerDep, SessionDep
from aiva_gateway.api.errors import ERROR_RESPONSES
from aiva_gateway.auth.principal import Caller
from aiva_gateway.auth.rbac import Permission, require
from aiva_gateway.contracts.runs import ModelAttemptOut, PolicyEventOut, RunDetail, RunTrace, TraceSpanOut
from aiva_gateway.core.enums import CimState, ExecutionPath, Outcome, Role, RunStatus
from aiva_gateway.core.errors import NotFoundError
from aiva_gateway.db.models import ExecutionRunRow, ModelAttemptRow, PolicyEventRow, TraceSpanRow

router = APIRouter(tags=["runs"])


def can_read_run(caller: Caller, run: ExecutionRunRow) -> bool:
    if caller.tenant_id != run.tenant_id:
        return False
    if caller.kind == "application":
        return run.application_id == caller.application_id
    if caller.has_any(Role.PLATFORM_ADMIN, Role.TENANT_ADMIN, Role.SECURITY_AUDITOR):
        return True
    # A public demo session reads its own runs and application-initiated ones, never another user's or
    # another visitor's (ADR-0019).
    if caller.has_any(Role.PUBLIC_VISITOR) and run.principal_pseudonym is None:
        return True
    if caller.has_any(Role.DEPARTMENT_MANAGER) and run.department_id == caller.department_id:
        return True
    if caller.has_any(Role.APPLICATION_OWNER) and run.application_id in caller.owned_application_ids:
        return True
    return (
        caller.has_any(Role.TESTER, Role.PUBLIC_VISITOR)
        and run.principal_pseudonym is not None
        and (run.principal_pseudonym == caller.pseudonym)
    )


async def _readable_run(run_id: str, caller: Caller, session: SessionDep) -> ExecutionRunRow:
    if caller.kind == "user":
        require(caller, Permission.RUNS_READ)
    run = await session.get(ExecutionRunRow, run_id)
    if run is None or not can_read_run(caller, run):
        raise NotFoundError("Run not found.")
    return run


@router.get(
    "/v1/runs/{run_id}",
    response_model=RunDetail,
    responses={**ERROR_RESPONSES, 404: ERROR_RESPONSES[403]},
    summary="Run detail with attempts and policy events",
)
async def get_run(run_id: str, caller: CallerDep, session: SessionDep) -> RunDetail:
    run = await _readable_run(run_id, caller, session)
    attempts = (
        await session.execute(
            select(ModelAttemptRow).where(ModelAttemptRow.run_id == run_id).order_by(ModelAttemptRow.sequence)
        )
    ).scalars()
    events = (
        await session.execute(
            select(PolicyEventRow).where(PolicyEventRow.run_id == run_id).order_by(PolicyEventRow.sequence)
        )
    ).scalars()
    return RunDetail(
        run_id=run.run_id,
        trace_id=run.trace_id,
        tenant_id=run.tenant_id,
        application_id=run.application_id,
        scenario_id=run.scenario_id,
        status=RunStatus(run.status),
        outcome=Outcome(run.outcome),
        execution_path=ExecutionPath(run.execution_path),
        selected_model=run.selected_model,
        cim_state=CimState(run.cim_state),
        cache_hit=run.cache_hit,
        identity_mode=run.identity_mode,
        source_mode=run.source_mode,
        policy_version=run.policy_version,
        data_designation=run.data_designation,
        created_at=run.created_at,
        completed_at=run.completed_at,
        latency_ms=run.latency_ms,
        attempts=[
            ModelAttemptOut(
                attempt_id=a.attempt_id,
                stage=a.stage,
                model_id=a.model_id,
                provider=a.provider,
                status=a.status,
                input_tokens=a.input_tokens,
                output_tokens=a.output_tokens,
                cached_input_tokens=a.cached_input_tokens,
                latency_ms=a.latency_ms,
                provenance=a.provenance,
            )
            for a in attempts
        ],
        policy_events=[
            PolicyEventOut(
                stage=e.stage,
                tool_id=e.tool_id,
                action=e.action,
                policy_version=e.policy_version,
                bundle_hash=e.bundle_hash,
                matched_rule_ids=e.matched_rule_ids,
                effective_classification=e.effective_classification,
            )
            for e in events
        ],
    )


@router.get(
    "/v1/runs/{run_id}/trace",
    response_model=RunTrace,
    responses={**ERROR_RESPONSES, 404: ERROR_RESPONSES[403]},
    summary="Redacted execution trace",
)
async def get_run_trace(run_id: str, caller: CallerDep, session: SessionDep) -> RunTrace:
    run = await _readable_run(run_id, caller, session)
    spans = (
        await session.execute(
            select(TraceSpanRow).where(TraceSpanRow.run_id == run_id).order_by(TraceSpanRow.started_at)
        )
    ).scalars()
    return RunTrace(
        run_id=run.run_id,
        trace_id=run.trace_id,
        spans=[
            TraceSpanOut(
                span_id=s.span_id,
                parent_span_id=s.parent_span_id,
                name=s.name,
                status=s.status,
                started_at=s.started_at,
                duration_ms=s.duration_ms,
                attributes=s.attributes,
            )
            for s in spans
        ],
    )
