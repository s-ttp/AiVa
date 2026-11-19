"""Budgets, FinOps alerts and ledger reconciliation (LLD §8, §12, §21.9). Delivered by FinOps (P3)."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query

from aiva_gateway.api.deps import CallerDep, ServicesDep, SessionDep
from aiva_gateway.api.errors import ERROR_RESPONSES
from aiva_gateway.auth.rbac import Permission, require
from aiva_gateway.contracts.admin import (
    BudgetAlertList,
    BudgetIn,
    BudgetList,
    BudgetStatus,
    ReconciliationReport,
)
from aiva_gateway.contracts.common import ErrorEnvelope
from aiva_gateway.contracts.demo import LiveBudget
from aiva_gateway.core.errors import NotImplementedYetError
from aiva_gateway.db.models import AuditLogRow
from aiva_gateway.finops import admin
from aiva_gateway.finops.live import LiveRunWindow
from aiva_gateway.finops.quota import RedisQuotaService
from aiva_gateway.finops.reconcile import Reconciler

router = APIRouter(tags=["budgets"])
_UNAVAILABLE: dict[int | str, dict[str, Any]] = {
    503: {"model": ErrorEnvelope, "description": "The quota store is unavailable."}
}


@router.get(
    "/v1/admin/budgets",
    response_model=BudgetList,
    responses=ERROR_RESPONSES,
    summary="Budgets with live status: cap, spent, reserved, forecast, thresholds and remaining",
)
async def list_budgets(caller: CallerDep, services: ServicesDep, session: SessionDep) -> BudgetList:
    require(caller, Permission.BUDGETS_READ)
    now = datetime.now(UTC)
    budgets = await admin.budget_statuses(
        services.quota,
        session,
        services.catalog,
        caller,
        now=now,
        inference_mode=services.settings.inference_mode,
    )
    return BudgetList(
        budgets=budgets, last_reconciliation=await admin.last_reconciliation(session, caller.tenant_id)
    )


@router.post(
    "/v1/admin/budgets",
    response_model=BudgetStatus,
    status_code=201,
    responses={**ERROR_RESPONSES, **_UNAVAILABLE},
    summary="Create or update a budget (audited; enforced from the next reservation)",
)
async def upsert_budget(
    body: BudgetIn, caller: CallerDep, services: ServicesDep, session: SessionDep
) -> BudgetStatus:
    require(caller, Permission.BUDGETS_WRITE)
    now = datetime.now(UTC)
    # Authentication already opened the session's transaction; the upsert and its audit commit together.
    budget_id = await admin.upsert_budget(session, services.catalog, services.settings, caller, body, now=now)
    await session.commit()
    await admin.publish(services.quota, caller.tenant_id)
    statuses = await admin.budget_statuses(
        services.quota,
        session,
        services.catalog,
        caller,
        now=now,
        inference_mode=services.settings.inference_mode,
        only_id=budget_id,
    )
    return statuses[0]


@router.get(
    "/v1/admin/budgets/alerts",
    response_model=BudgetAlertList,
    responses=ERROR_RESPONSES,
    summary="FinOps alerts: 80%/95% thresholds, hard caps, overruns and reconciliation findings",
)
async def list_alerts(
    caller: CallerDep,
    services: ServicesDep,
    session: SessionDep,
    since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> BudgetAlertList:
    require(caller, Permission.BUDGETS_READ)
    alerts = await admin.list_alerts(session, services.catalog, caller, since=since, limit=limit)
    return BudgetAlertList(alerts=alerts)


@router.post(
    "/v1/admin/budgets/reconcile",
    response_model=ReconciliationReport,
    responses={**ERROR_RESPONSES, 409: ERROR_RESPONSES[403], 501: ERROR_RESPONSES[403], **_UNAVAILABLE},
    summary="Reconcile reservations with the usage ledger now (also runs on a schedule)",
)
async def reconcile(caller: CallerDep, services: ServicesDep, session: SessionDep) -> ReconciliationReport:
    require(caller, Permission.BUDGETS_WRITE)
    if not isinstance(services.quota, RedisQuotaService):
        raise NotImplementedYetError("Reconciliation needs the Redis quota store.")
    reconciler = Reconciler(
        services.quota,
        services.sessionmaker,
        services.catalog,
        stale_after=services.settings.reservation_stale,
    )
    actor = caller.actor_ref
    result = await reconciler.run(caller.tenant_id, triggered_by=actor)
    session.add(
        AuditLogRow(
            tenant_id=caller.tenant_id,
            actor_kind="user",
            actor_id=actor,
            action="budget.reconcile",
            target_type="reconciliation",
            target_id=result.reconciliation_id,
            details={"status": result.status, "findings": len(result.findings)},
        )
    )
    await session.commit()
    return admin.report_of(result)


@router.get(
    "/v1/demo/live-budget",
    response_model=LiveBudget,
    responses={**ERROR_RESPONSES, **_UNAVAILABLE},
    tags=["demo"],
    summary="Public demo: how much of today's public live budget is left, and this visitor's live runs",
)
async def live_budget(caller: CallerDep, services: ServicesDep, session: SessionDep) -> LiveBudget:
    settings = services.settings
    window = LiveRunWindow(
        services.redis,
        limit=settings.public_live_runs_per_window,
        window=timedelta(seconds=settings.public_live_window_seconds),
    )
    return await admin.live_budget(services.quota, session, window, caller, now=datetime.now(UTC))
