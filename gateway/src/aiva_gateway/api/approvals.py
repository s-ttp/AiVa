"""AiVa Govern Approval Queue (ADR-0012). Approvers list and decide; requesters poll their own request."""

from typing import Annotated, Literal

from fastapi import APIRouter, Query
from sqlalchemy import select

from aiva_gateway.api.deps import CallerDep, ServicesDep, SessionDep
from aiva_gateway.api.errors import ERROR_RESPONSES
from aiva_gateway.auth.principal import Caller
from aiva_gateway.auth.rbac import Permission, require
from aiva_gateway.contracts.common import ErrorEnvelope
from aiva_gateway.contracts.govern import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ApprovalList,
    ApprovalRequestOut,
)
from aiva_gateway.core.errors import NotFoundError
from aiva_gateway.db.models import ApprovalRequestRow, ExecutionRunRow
from aiva_gateway.govern import approvals
from aiva_gateway.services import Services

router = APIRouter(tags=["approvals"])
_RESPONSES = {
    **ERROR_RESPONSES,
    404: {"model": ErrorEnvelope, "description": "Not found or not visible to the caller."},
}
_DECIDE_RESPONSES = {
    **_RESPONSES,
    409: {"model": ErrorEnvelope, "description": "Already decided or expired."},
    503: {"model": ErrorEnvelope, "description": "Policy engine unavailable; nothing was changed."},
}


async def _visible(
    approval_id: str, caller: Caller, session: SessionDep, services: Services, *, lock: bool = False
) -> tuple[ApprovalRequestRow, str | None]:
    row = await session.get(ApprovalRequestRow, approval_id, with_for_update=lock)
    if row is None or not approvals.can_read(caller, row, services.catalog):
        raise NotFoundError("Approval request not found.")
    run = await session.get(ExecutionRunRow, row.run_id)
    return row, run.ingress_source_policy if run else None


@router.get(
    "/v1/admin/approvals",
    response_model=ApprovalList,
    responses=_RESPONSES,
    summary="Approval Queue: requests visible to the caller, newest first",
)
async def list_approvals(
    caller: CallerDep,
    session: SessionDep,
    services: ServicesDep,
    status: Annotated[
        Literal["pending", "approved", "denied", "expired", "consumed"] | None, Query()
    ] = "pending",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ApprovalList:
    require(caller, Permission.APPROVALS_READ)
    await approvals.expire_stale(session, caller.tenant_id)
    await session.commit()
    query = (
        select(ApprovalRequestRow, ExecutionRunRow.ingress_source_policy)
        .join(ExecutionRunRow, ExecutionRunRow.run_id == ApprovalRequestRow.run_id)
        .where(ApprovalRequestRow.tenant_id == caller.tenant_id)
        .order_by(ApprovalRequestRow.requested_at.desc())
    )
    if status is not None:
        query = query.where(ApprovalRequestRow.status == status)
    rows = (await session.execute(query)).all()
    visible = [
        approvals.to_out(row, matched_network_id=network, viewer=caller)
        for row, network in rows
        if approvals.can_read(caller, row, services.catalog)
    ]
    return ApprovalList(approvals=visible[:limit])


@router.get(
    "/v1/approvals/{approval_id}",
    response_model=ApprovalRequestOut,
    responses=_RESPONSES,
    summary="Approval status for the requester (application or tester) and approvers",
)
async def get_approval(
    approval_id: str, caller: CallerDep, session: SessionDep, services: ServicesDep
) -> ApprovalRequestOut:
    row, network = await _visible(approval_id, caller, session, services)
    return approvals.to_out(row, matched_network_id=network, viewer=caller)


async def _decide(
    approval_id: str,
    body: ApprovalDecisionRequest,
    caller: Caller,
    session: SessionDep,
    services: Services,
    *,
    approve: bool,
) -> ApprovalDecisionResponse:
    require(caller, Permission.APPROVALS_DECIDE)
    row, network = await _visible(approval_id, caller, session, services, lock=True)
    result = await approvals.decide(
        services.policy_engine, session, row, caller, approve=approve, note=body.note
    )
    await session.commit()
    result.approval.matched_network_id = network
    return result


@router.post(
    "/v1/admin/approvals/{approval_id}/approve",
    response_model=ApprovalDecisionResponse,
    responses=_DECIDE_RESPONSES,
    summary="Approve after re-evaluating against the current policy version",
)
async def approve(
    approval_id: str,
    body: ApprovalDecisionRequest,
    caller: CallerDep,
    session: SessionDep,
    services: ServicesDep,
) -> ApprovalDecisionResponse:
    return await _decide(approval_id, body, caller, session, services, approve=True)


@router.post(
    "/v1/admin/approvals/{approval_id}/deny",
    response_model=ApprovalDecisionResponse,
    responses=_DECIDE_RESPONSES,
    summary="Deny and close an approval request",
)
async def deny(
    approval_id: str,
    body: ApprovalDecisionRequest,
    caller: CallerDep,
    session: SessionDep,
    services: ServicesDep,
) -> ApprovalDecisionResponse:
    return await _decide(approval_id, body, caller, session, services, approve=False)
