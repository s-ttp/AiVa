"""Agentic workflows (LLD §15 #6).

Each model step is a governed chat run sharing the workflow's budget envelope; every tool call passes the
`tool` policy stage first (ADR-0012). Row-level read scope matches run drilldown (LLD §21.8): unreadable
workflows return 404.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from aiva_gateway.agents.service_incident import ServiceIncidentAgent
from aiva_gateway.agents.store import (
    can_read_workflow,
    clear_pause,
    load_pause,
    load_workflow_out,
    readable_workflows,
    resumed_workflow_out,
    save_pause,
    save_workflow,
    verify_pause,
    workflow_budget,
)
from aiva_gateway.agents.store import workflow_out as build_workflow_out
from aiva_gateway.api import idempotency
from aiva_gateway.api.deps import CallerDep, RunCallerDep, ServicesDep, SessionDep, client_ip
from aiva_gateway.api.errors import ERROR_RESPONSES
from aiva_gateway.auth.principal import RunIdentity
from aiva_gateway.auth.rbac import Permission, require
from aiva_gateway.auth.run_identity import resolve_run_identity
from aiva_gateway.contracts.agents import (
    AgentWorkflowList,
    AgentWorkflowOut,
    AgentWorkflowRequest,
    AgentWorkflowResumeRequest,
)
from aiva_gateway.contracts.common import ErrorEnvelope, PageInfo
from aiva_gateway.core.canonical import sha256_hex
from aiva_gateway.core.errors import ConflictError, NotFoundError
from aiva_gateway.db.models import AgentStepRow, AgentWorkflowRow
from aiva_gateway.services import Services

router = APIRouter(tags=["agents"])
_RESPONSES: dict[int | str, dict[str, object]] = {
    **ERROR_RESPONSES,
    404: {"model": ErrorEnvelope, "description": "Not found or not readable by the caller."},
}
_TOOL_FAILED: dict[int | str, dict[str, object]] = {
    502: {
        "model": AgentWorkflowOut,
        "description": "A synthetic system failed to run an allowed tool call; the call is not retried.",
    },
}


@router.post(
    "/v1/agents/workflows",
    response_model=AgentWorkflowOut,
    summary="Run a multi-step agent workflow under per-tool policy and one workflow budget",
    responses={
        **_RESPONSES,
        **_TOOL_FAILED,
        503: {"model": AgentWorkflowOut, "description": "A dependency was unavailable; failed closed."},
    },
)
async def create_workflow(
    body: AgentWorkflowRequest, request: Request, caller: RunCallerDep, services: ServicesDep
) -> JSONResponse:
    identity = resolve_run_identity(
        caller, body.test_profile, client_ip(request, services), services.settings
    )
    agent = ServiceIncidentAgent(services)
    wf = agent.new_workflow(body, identity, caller.tenant_id)
    await agent.run(wf)
    async with services.sessionmaker() as session, session.begin():
        await save_workflow(session, wf)
    await save_pause(services, wf)
    async with services.sessionmaker() as session:
        budget = await workflow_budget(
            services,
            session,
            wf.tenant_id,
            identity.application_id,
            wf.workflow_id,
            identity.principal_pseudonym,
        )
    content = build_workflow_out(wf, budget).model_dump(mode="json")
    return JSONResponse(status_code=wf.http_status, content=content)


@router.get(
    "/v1/agents/workflows",
    response_model=AgentWorkflowList,
    summary="Agent workflows readable by the caller, newest first",
    responses=_RESPONSES,
)
async def list_workflows(
    caller: CallerDep,
    services: ServicesDep,
    session: SessionDep,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> AgentWorkflowList:
    if caller.kind == "user":
        require(caller, Permission.RUNS_READ)
    # Workflow IDs are ULIDs: ID order is creation order, and the last ID is the cursor.
    query = select(AgentWorkflowRow).where(
        AgentWorkflowRow.tenant_id == caller.tenant_id, readable_workflows(caller)
    )
    if cursor is not None:
        query = query.where(AgentWorkflowRow.workflow_id < cursor)
    rows = list(
        (
            await session.execute(query.order_by(AgentWorkflowRow.workflow_id.desc()).limit(limit + 1))
        ).scalars()
    )
    page = rows[:limit]
    items = [await load_workflow_out(services, session, row) for row in page]
    next_cursor = page[-1].workflow_id if len(rows) > limit else None
    return AgentWorkflowList(items=items, page=PageInfo(next_cursor=next_cursor, limit=limit))


@router.get(
    "/v1/agents/workflows/{workflow_id}",
    response_model=AgentWorkflowOut,
    summary="Agent workflow detail: steps, tool decisions and budget (no tool or model text)",
    responses=_RESPONSES,
)
async def get_workflow(
    workflow_id: str, caller: CallerDep, services: ServicesDep, session: SessionDep
) -> AgentWorkflowOut:
    if caller.kind == "user":
        require(caller, Permission.RUNS_READ)
    row = await session.get(AgentWorkflowRow, workflow_id)
    if row is None or not can_read_workflow(caller, row):
        raise NotFoundError("Workflow not found.")
    return await load_workflow_out(services, session, row)


@router.post(
    "/v1/agents/workflows/{workflow_id}/resume",
    response_model=AgentWorkflowOut,
    summary="Resume a workflow paused for approval: a budget approval re-runs the paused model step, a tool "
    "approval executes the recorded call; then continue",
    responses={
        **_RESPONSES,
        **_TOOL_FAILED,
        409: {
            "model": ErrorEnvelope,
            "description": "Not paused or pause expired, approval not usable (pending, used, expired) or "
            "not this workflow's, budget approval already used, or Idempotency-Key conflict.",
        },
        503: {"model": AgentWorkflowOut, "description": "A dependency was unavailable; failed closed."},
    },
)
async def resume_workflow(
    workflow_id: str,
    body: AgentWorkflowResumeRequest,
    request: Request,
    caller: RunCallerDep,
    services: ServicesDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
) -> JSONResponse:
    identity = resolve_run_identity(
        caller, body.test_profile, client_ip(request, services), services.settings
    )
    scope = (
        f"{caller.tenant_id}:{identity.application_id}:{identity.principal_pseudonym or 'app'}:{workflow_id}"
    )
    request_hash = sha256_hex(body.model_dump(mode="json", exclude={"test_profile"}))
    if idempotency_key:
        # A client retry replays the first result instead of resuming twice.
        replay = await idempotency.begin(services.redis, scope, idempotency_key, request_hash)
        if replay is not None:
            return JSONResponse(
                status_code=replay["status_code"],
                content=replay["body"],
                headers={"Idempotent-Replayed": "true"},
            )
    try:
        status_code, content = await _resume(workflow_id, body, identity, caller.tenant_id, services)
    except BaseException:
        if idempotency_key:
            await idempotency.abandon(services.redis, scope, idempotency_key)
        raise
    if idempotency_key:
        await idempotency.complete(services.redis, scope, idempotency_key, request_hash, status_code, content)
    return JSONResponse(status_code=status_code, content=content)


async def _resume(
    workflow_id: str,
    body: AgentWorkflowResumeRequest,
    identity: RunIdentity,
    tenant_id: str,
    services: Services,
) -> tuple[int, dict[str, Any]]:
    async with services.sessionmaker() as session:
        row = await session.get(AgentWorkflowRow, workflow_id)
        # Only the principal that ran a workflow can resume it (the approval is bound to it as well).
        if (
            row is None
            or row.tenant_id != tenant_id
            or row.application_id != identity.application_id
            or row.principal_kind != identity.principal_kind
            or row.principal_pseudonym != identity.principal_pseudonym
        ):
            raise NotFoundError("Workflow not found.")
        paused = await load_pause(services, tenant_id, workflow_id)
        if paused is None:
            raise ConflictError(
                "This workflow is not waiting for approval, or its pause has expired.",
                code="WORKFLOW_NOT_PAUSED",
                remediation="Start a new workflow.",
            )
        agent_request, pause = paused
        if body.approval_id != pause.approval_id:
            raise ConflictError(
                "The approval does not belong to this workflow's paused step.",
                code="APPROVAL_REQUEST_MISMATCH",
                remediation=f"Resume with the pending approval {pause.approval_id}.",
            )
        await verify_pause(session, workflow_id, row.incident_id, pause)
        step_count = await session.scalar(
            select(func.count()).select_from(AgentStepRow).where(AgentStepRow.workflow_id == workflow_id)
        )
        denied = await session.scalar(
            select(func.count())
            .select_from(AgentStepRow)
            .where(AgentStepRow.workflow_id == workflow_id, AgentStepRow.status == "denied")
        )
        created_at = row.created_at
    agent = ServiceIncidentAgent(services)
    wf = agent.new_workflow(agent_request, identity, tenant_id, workflow_id=workflow_id)
    wf.created_at, wf.first_index, wf.prior_denials = created_at, int(step_count or 0), bool(denied)
    # Govern's claim (approval pending, consumed by a concurrent resume or expired) raises 409 here,
    # before any step runs: exactly one resume wins.
    await agent.run(wf, resume=pause)
    async with services.sessionmaker() as session, session.begin():
        await save_workflow(session, wf)
    if wf.pause is not None:
        await save_pause(services, wf)
    elif not (wf.error is not None and wf.error.retryable):
        # A retryable failure released the approval: the same pause can be resumed again.
        await clear_pause(services, tenant_id, workflow_id)
    if wf.error is not None and wf.error.code == "BUDGET_APPROVAL_USED":
        # FinOps' single-use backstop (ADR-0014): the approval already funded a reservation.
        raise ConflictError(wf.error.message, code=wf.error.code, remediation="Start a new workflow.")
    async with services.sessionmaker() as session:
        stored = await session.get(AgentWorkflowRow, workflow_id)
        assert stored is not None
        content = (await resumed_workflow_out(services, session, stored, wf)).model_dump(mode="json")
    return wf.http_status, content
