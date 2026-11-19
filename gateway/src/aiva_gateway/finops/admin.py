"""Budget administration: live status, validated upserts and alert listing (LLD §8, §21.8, §22.6).

Row-level scope is enforced here, in the query layer: finance and administrators see every budget of their
tenant; a department manager sees their department's budgets and those of its applications; an application
owner sees their applications' budgets. Spent figures come from the live counters, or from the Postgres
journal when the quota store is unreachable (the journal is the system of record either way). Each status
reports the money domain of the gateway's configured inference mode first and the other domain alongside
(ADR-0008 amendment); illustrative figures are never labelled `measured` or `estimated`.
"""

import re
from collections.abc import Sequence
from datetime import datetime

from redis.exceptions import RedisError
from sqlalchemy import ColumnElement, select, true
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aiva_gateway.auth.principal import Caller
from aiva_gateway.auth.tokens import pseudonymise
from aiva_gateway.catalog import Catalog
from aiva_gateway.config import Settings
from aiva_gateway.contracts.admin import (
    BudgetAlert,
    BudgetDomain,
    BudgetDomainUsage,
    BudgetIn,
    BudgetStatus,
    ReconciliationFinding,
    ReconciliationReport,
    ReconciliationSummary,
)
from aiva_gateway.contracts.demo import LiveBudget, LiveDemoReason
from aiva_gateway.core.enums import Provenance, Role
from aiva_gateway.core.errors import DependencyUnavailableError, ValidationFailedError
from aiva_gateway.db import models as db
from aiva_gateway.finops.alerts import admin_alerts, alert_visibility
from aiva_gateway.finops.budgets import (
    DEFAULT_SCOPE,
    VALID_PERIODS,
    BudgetDef,
    budget_id,
    forecast_micros,
    from_bps,
    load_book,
    to_bps,
)
from aiva_gateway.finops.ledger import bucket_totals
from aiva_gateway.finops.live import LiveRunWindow
from aiva_gateway.finops.pricing import gpu_pools
from aiva_gateway.finops.quota import BucketState, RedisQuotaService
from aiva_gateway.finops.reconcile import ReconciliationResult
from aiva_gateway.finops.scopes import (
    ILLUSTRATIVE,
    INTERNET_GPU,
    LIVE,
    MONEY,
    PUBLIC_DEMO,
    PUBLIC_VISITOR_ROLE,
    Bucket,
    application_of,
    configured_domain,
    domain_kind,
    period_bounds,
    period_key,
)
from aiva_gateway.pipeline.interfaces import QuotaService

_WORKFLOW_ID = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
_SEE_ALL = (Role.PLATFORM_ADMIN, Role.TENANT_ADMIN, Role.FINANCE_ANALYST)


# --- Row-level scope -------------------------------------------------------------------------------


def can_see(caller: Caller, catalog: Catalog, scope_type: str | None, scope_id: str | None) -> bool:
    if caller.has_any(*_SEE_ALL):
        return True
    if scope_type is None or scope_id is None:
        return False
    application = application_of(scope_type, scope_id)
    if caller.has_any(Role.DEPARTMENT_MANAGER) and caller.department_id:
        if scope_type == "department" and scope_id == caller.department_id:
            return True
        if application and catalog.department_of(application) == caller.department_id:
            return True
    return bool(caller.has_any(Role.APPLICATION_OWNER) and application in caller.owned_application_ids)


def _visible_applications(caller: Caller, catalog: Catalog) -> set[str]:
    apps = set()
    if caller.has_any(Role.DEPARTMENT_MANAGER) and caller.department_id:
        apps |= {a.id for a in catalog.tenant.applications if a.department_id == caller.department_id}
    if caller.has_any(Role.APPLICATION_OWNER):
        apps |= set(caller.owned_application_ids)
    return apps


def alert_scope_filter(caller: Caller, catalog: Catalog) -> ColumnElement[bool]:
    """SQL form of `can_see` for alerts, so limits apply after row-level filtering."""
    if caller.has_any(*_SEE_ALL):
        return true()
    manager = caller.has_any(Role.DEPARTMENT_MANAGER) and caller.department_id
    return alert_visibility(_visible_applications(caller, catalog), caller.department_id if manager else None)


# --- Status ----------------------------------------------------------------------------------------


def current_bucket(budget: BudgetDef, now: datetime, *, domain: str) -> Bucket | None:
    """The bucket a budget is enforced on right now in `domain`; None for templates and per-request
    ceilings."""
    if budget.is_template or budget.period == "request":
        return None
    return Bucket(budget.scope_type, budget.scope_id, budget.period, period_key(budget.period, now), domain)


def domain_label(state: BucketState | None, domain: str) -> Provenance:
    """A bucket's spent label. Illustrative spend is never presented as money (ADR-0006, ADR-0008)."""
    if domain == LIVE:
        return state.provenance if state else Provenance.MEASURED
    label = state.provenance if state else Provenance.SIMULATED
    return Provenance.SIMULATED if label in MONEY else label


async def bucket_states(
    quota: QuotaService, session: AsyncSession, tenant_id: str, buckets: Sequence[Bucket]
) -> dict[str, BucketState]:
    if isinstance(quota, RedisQuotaService):
        try:
            return await quota.bucket_states(tenant_id, buckets)
        except (RedisError, OSError):
            pass  # the journal is the system of record; report from it
    totals = await bucket_totals(session, tenant_id, [b.key for b in buckets])
    return {key: BucketState(t.spent, t.reserved, t.labels) for key, t in totals.items()}


def status_of(
    budget: BudgetDef,
    state: BucketState | None,
    bucket: Bucket | None,
    now: datetime,
    *,
    domain: str,
    other: BudgetDomainUsage | None = None,
) -> BudgetStatus:
    spent = state.spent if state else 0
    reserved = state.reserved if state else 0
    forecast = forecast_micros(budget.period, spent, now) if bucket else None
    return BudgetStatus(
        id=budget.id,
        scope_type=budget.scope_type,
        scope_id=budget.scope_id,
        period=budget.period,
        cap_micros=budget.cap_micros,
        alert_thresholds=[from_bps(b) for b in budget.thresholds_bps],
        on_exhaustion=budget.on_exhaustion,
        period_key=bucket.period_key if bucket else None,
        spent_micros=spent,
        reserved_micros=reserved,
        remaining_micros=max(0, budget.cap_micros - spent - reserved),
        forecast_micros=forecast,
        forecast_provenance=Provenance.ESTIMATED if forecast is not None else None,
        thresholds_crossed=[from_bps(b) for b in budget.crossed(spent)] if bucket else [],
        provenance=domain_label(state, domain),
        domain=_contract_domain(domain),
        other_domain=other,
    )


def _contract_domain(domain: str) -> BudgetDomain:
    return "live" if domain == LIVE else "illustrative"


def usage_of(budget: BudgetDef, state: BucketState | None, domain: str) -> BudgetDomainUsage | None:
    """A budget's usage in a secondary domain, or None when that domain has none this period."""
    if state is None or not (state.spent or state.reserved):
        return None
    return BudgetDomainUsage(
        domain=_contract_domain(domain),
        spent_micros=state.spent,
        reserved_micros=state.reserved,
        remaining_micros=max(0, budget.cap_micros - state.spent - state.reserved),
        provenance=domain_label(state, domain),
    )


async def budget_statuses(
    quota: QuotaService,
    session: AsyncSession,
    catalog: Catalog,
    caller: Caller,
    *,
    now: datetime,
    inference_mode: str,
    only_id: str | None = None,
) -> list[BudgetStatus]:
    book = await load_book(session, caller.tenant_id)
    budgets = [
        b
        for b in book.budgets
        if can_see(caller, catalog, b.scope_type, b.scope_id) and (only_id is None or b.id == only_id)
    ]
    primary = configured_domain(inference_mode)
    secondary = ILLUSTRATIVE if primary == LIVE else LIVE
    buckets = {b.id: bucket for b in budgets if (bucket := current_bucket(b, now, domain=primary))}
    others = {b.id: bucket for b in budgets if (bucket := current_bucket(b, now, domain=secondary))}
    states = await bucket_states(quota, session, caller.tenant_id, [*buckets.values(), *others.values()])
    statuses = []
    for b in budgets:
        bucket, other = buckets.get(b.id), others.get(b.id)
        state = states.get(bucket.key) if bucket else None
        usage = usage_of(b, states.get(other.key), secondary) if other else None
        statuses.append(status_of(b, state, bucket, now, domain=primary, other=usage))
    return statuses


async def live_budget(
    quota: QuotaService,
    session: AsyncSession,
    window: LiveRunWindow,
    caller: Caller,
    *,
    now: datetime,
) -> LiveBudget:
    """The public demo's live-run readout (ADR-0019): coarse by design, no amounts."""
    bucket = Bucket(PUBLIC_DEMO, caller.tenant_id, "daily", period_key("daily", now), LIVE)
    book = await load_book(session, caller.tenant_id)
    budget = book.for_bucket(bucket)
    state = (await bucket_states(quota, session, caller.tenant_id, [bucket])).get(bucket.key)
    percent, budget_left = None, False
    if budget is not None:
        left = max(0, budget.cap_micros - (state.spent + state.reserved if state else 0))
        percent = left * 100 // budget.cap_micros // 10 * 10 if budget.cap_micros else 0
        budget_left = left > 0
    runs_left, runs_reset = None, None
    if PUBLIC_VISITOR_ROLE in caller.roles and caller.pseudonym:
        visitor = await window.state(caller.tenant_id, caller.pseudonym)
        runs_left, runs_reset = visitor.remaining, visitor.resets_at
    reason: LiveDemoReason | None = None
    if not budget_left:
        reason = "budget"
    elif runs_left == 0:
        reason = "visitor_limit"
    bounds = period_bounds("daily", now)
    assert bounds is not None
    switched_off = budget is None or budget.cap_micros == 0
    return LiveBudget(
        available=reason is None,
        remaining_percent=percent,
        resets_at=None if switched_off else bounds[1],
        provenance=domain_label(state, LIVE),
        visitor_runs_remaining=runs_left,
        visitor_resets_at=runs_reset,
        unavailable_reason=reason,
    )


async def last_reconciliation(session: AsyncSession, tenant_id: str) -> ReconciliationSummary | None:
    row = await session.scalar(
        select(db.ReconciliationRunRow)
        .where(db.ReconciliationRunRow.tenant_id == tenant_id)
        .order_by(db.ReconciliationRunRow.started_at.desc())
        .limit(1)
    )
    if row is None:
        return None
    return ReconciliationSummary(
        reconciliation_id=row.reconciliation_id,
        status=row.status,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


# --- Upsert ----------------------------------------------------------------------------------------


async def _normalise_scope(
    body: BudgetIn, session: AsyncSession, catalog: Catalog, settings: Settings, tenant_id: str
) -> str:
    """Validate the scope reference and return the canonical scope ID (users by pseudonym)."""
    scope_type, scope_id = body.scope_type, body.scope_id
    if body.period not in VALID_PERIODS[scope_type]:
        allowed = ", ".join(sorted(VALID_PERIODS[scope_type]))
        raise ValidationFailedError(f"A {scope_type} budget needs period {allowed}.")
    if scope_id == DEFAULT_SCOPE:
        if scope_type in ("tenant", PUBLIC_DEMO, INTERNET_GPU):
            raise ValidationFailedError(f"A {scope_type.replace('_', '-')} budget must name the tenant.")
        return scope_id
    known: bool
    match scope_type:
        case "tenant" | "public_demo" | "internet_gpu":
            known = scope_id == tenant_id
        case "department":
            known = any(d.id == scope_id for d in catalog.tenant.departments)
        case "application" | "request":
            known = catalog.application(scope_id) is not None
        case "model":
            known = catalog.model(scope_id) is not None
        case "gpu_pool":
            known = scope_id in set(gpu_pools(catalog).values())
        case "workflow":
            app, _, workflow = scope_id.partition("/")
            known = catalog.application(app) is not None and bool(_WORKFLOW_ID.match(workflow))
        case "user":
            pseudonym = scope_id
            if not scope_id.startswith("usr_"):
                pseudonym = pseudonymise(settings.pseudonym_key.get_secret_value(), scope_id)
            known = bool(
                await session.scalar(
                    select(db.PrincipalRow.pseudonym).where(
                        db.PrincipalRow.tenant_id == tenant_id, db.PrincipalRow.pseudonym == pseudonym
                    )
                )
            )
            scope_id = pseudonym
    if not known:
        raise ValidationFailedError(f"Unknown {scope_type} '{body.scope_id}'.")
    return scope_id


def _thresholds(values: Sequence[float]) -> list[float]:
    bps = sorted({to_bps(v) for v in values})
    if any(b <= 0 or b >= 10_000 for b in bps):
        raise ValidationFailedError("Alert thresholds must be fractions strictly between 0 and 1.")
    return [from_bps(b) for b in bps]


async def upsert_budget(
    session: AsyncSession,
    catalog: Catalog,
    settings: Settings,
    caller: Caller,
    body: BudgetIn,
    *,
    now: datetime,
) -> str:
    """Create or update a budget and audit the change. Returns its ID. The caller commits."""
    tenant_id = caller.tenant_id
    scope_id = await _normalise_scope(body, session, catalog, settings, tenant_id)
    thresholds = _thresholds(body.alert_thresholds)
    previous = await session.scalar(
        select(db.BudgetRow).where(
            db.BudgetRow.tenant_id == tenant_id,
            db.BudgetRow.scope_type == body.scope_type,
            db.BudgetRow.scope_id == scope_id,
            db.BudgetRow.period == body.period,
        )
    )
    identifier = previous.id if previous else budget_id(body.scope_type, scope_id, body.period)
    actor = caller.actor_ref
    values = {
        "id": identifier,
        "tenant_id": tenant_id,
        "scope_type": body.scope_type,
        "scope_id": scope_id,
        "period": body.period,
        "cap_micros": body.cap_micros,
        "alert_thresholds": thresholds,
        "on_exhaustion": body.on_exhaustion,
        "created_at": now,
        "updated_at": now,
        "updated_by": actor,
    }
    statement = insert(db.BudgetRow).values(**values)
    await session.execute(
        statement.on_conflict_do_update(
            constraint="uq_budget_tenant_id",
            set_={
                k: statement.excluded[k]
                for k in ("cap_micros", "alert_thresholds", "on_exhaustion", "updated_at", "updated_by")
            },
        )
    )
    session.add(
        db.AuditLogRow(
            tenant_id=tenant_id,
            actor_kind="user",
            actor_id=actor,
            action="budget.update" if previous else "budget.create",
            target_type="budget",
            target_id=identifier,
            details={
                "scope_type": body.scope_type,
                "scope_id": scope_id,
                "period": body.period,
                "cap_micros": body.cap_micros,
                "previous_cap_micros": previous.cap_micros if previous else None,
                "alert_thresholds": thresholds,
                "on_exhaustion": body.on_exhaustion,
            },
        )
    )
    return identifier


async def publish(quota: QuotaService, tenant_id: str) -> None:
    """Make every gateway process enforce the committed budgets from its next reservation."""
    if not isinstance(quota, RedisQuotaService):
        return
    try:
        await quota.publish_budgets(tenant_id)
    except (RedisError, OSError) as exc:
        raise DependencyUnavailableError(
            "The budget was saved but the quota store could not be updated; it is not enforced yet.",
            code="QUOTA_STORE_UNAVAILABLE",
            remediation="Repeat the same request to publish it.",
        ) from exc


# --- Alerts and reconciliation ------------------------------------------------------------------


async def list_alerts(
    session: AsyncSession, catalog: Catalog, caller: Caller, *, since: datetime | None, limit: int
) -> list[BudgetAlert]:
    alert = db.BudgetAlertRow
    statement = select(alert).where(
        alert.tenant_id == caller.tenant_id, admin_alerts(), alert_scope_filter(caller, catalog)
    )
    if since is not None:
        statement = statement.where(alert.created_at >= since)
    rows = await session.scalars(statement.order_by(alert.created_at.desc()).limit(limit))
    return [
        BudgetAlert(
            alert_id=row.alert_id,
            kind=row.kind,
            message=row.message,
            budget_id=row.budget_id,
            scope_type=row.scope_type,
            scope_id=row.scope_id,
            domain=_contract_domain(domain_kind(row.domain)) if row.domain else None,
            period=row.period,
            period_key=row.period_key,
            threshold=from_bps(row.threshold_bps) if row.threshold_bps is not None else None,
            cap_micros=row.cap_micros,
            spent_micros=row.spent_micros,
            amount_micros=row.amount_micros,
            reservation_id=row.reservation_id,
            run_id=row.run_id,
            provenance=Provenance(row.provenance),
            created_at=row.created_at,
        )
        for row in rows
    ]


def report_of(result: ReconciliationResult) -> ReconciliationReport:
    return ReconciliationReport(
        reconciliation_id=result.reconciliation_id,
        status=result.status,
        started_at=result.started_at,
        completed_at=result.completed_at,
        triggered_by=result.triggered_by,
        reservations_checked=result.reservations_checked,
        settled=result.settled,
        expired=result.expired,
        adjustments=result.adjustments,
        counters_corrected=result.counters_corrected,
        findings=[
            ReconciliationFinding(
                kind=f.kind,
                detail=f.detail,
                reservation_id=f.reservation_id,
                bucket=f.bucket,
                delta_micros=f.delta_micros,
            )
            for f in result.findings
        ],
        error=result.error,
    )
