"""Report queries (LLD §21.6). Grouped reports read the aggregates; drilldowns read the run records.

Every query applies the caller's row-level scope (reporting.scope) and the requested filters in SQL. Windows
are whole UTC days, inclusive; totals come from the daily aggregate, trends from the hourly aggregate for
windows of up to two days and from the daily aggregate otherwise.
"""

import base64
import binascii
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from typing import Any, Literal

from sqlalchemy import exists, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from aiva_gateway.catalog import Catalog
from aiva_gateway.contracts.reports import (
    AlertCount,
    Amount,
    CimOverhead,
    ClassificationReport,
    ClassificationRow,
    ClassificationTrendPoint,
    ConsumptionReport,
    ConsumptionRow,
    FilterOption,
    GovernanceReport,
    GovernanceRow,
    LatencySummary,
    ModelSuitability,
    OptimizationReport,
    OptimizationRow,
    OverviewReport,
    PolicyVersionChange,
    QualityKpi,
    ReportFilterOptions,
    ReportFilters,
    ReportMetadata,
    ReportPage,
    ReportWindow,
    RetrySummary,
    RuleCount,
    RunList,
    RunSummary,
    SeriesPoint,
    TaxonomyInfo,
    TrendPoint,
    ValueKpis,
)
from aiva_gateway.core.enums import Provenance
from aiva_gateway.core.errors import ValidationFailedError
from aiva_gateway.db import models as db
from aiva_gateway.finops.alerts import admin_alerts, alert_visibility
from aiva_gateway.reporting.dimensions import (
    SERVICE_PREFIX,
    UNATTRIBUTED,
    UNCLASSIFIED,
    run_dimension_columns,
)
from aiva_gateway.reporting.lines import MEASURES, run_label
from aiva_gateway.reporting.scope import ReportScope, shown_principal
from aiva_gateway.reporting.totals import Totals, ratio

MAX_WINDOW_DAYS = 366
HOURLY_TREND_DAYS = 2
MAX_SERIES_GROUPS = 8

# Report filter -> aggregate column.
FILTER_COLUMNS = {
    "department_id": "department_id",
    "application_id": "application_id",
    "user": "principal",
    "model_id": "model_id",
    "provider": "provider",
    "business_function": "business_function",
    "task_category": "task_category",
    "intent": "intent",
    "complexity": "complexity",
    "sensitivity": "sensitivity",
    "language": "language",
    "execution_path": "execution_path",
    "source_policy": "source_policy",
    "environment": "environment",
    "outcome": "outcome",
    "data_designation": "data_designation",
}
CONSUMPTION_GROUPS = {
    "application": "application_id",
    "department": "department_id",
    "user": "principal",
    "category": "task_category",
    "model": "model_id",
    "provider": "provider",
    "path": "execution_path",
}
CLASSIFICATION_GROUPS = {
    name: name
    for name in ("business_function", "task_category", "intent", "complexity", "sensitivity", "language")
}
OPTIMIZATION_GROUPS = {"application": "application_id", "strategy": "context_strategy", "model": "model_id"}
GOVERNANCE_GROUPS = {
    "policy": "policy_version",
    "application": "application_id",
    "source_policy": "source_policy",
}
GROUPS = {
    "consumption": CONSUMPTION_GROUPS,
    "classification": CLASSIFICATION_GROUPS,
    "optimization": OPTIMIZATION_GROUPS,
    "governance": GOVERNANCE_GROUPS,
}

# Line and run dimensions every grouped query returns, so totals can split by provenance and outcome.
_BASE = (
    "line_kind",
    "stage",
    "provenance",
    "outcome",
    "execution_path",
    "cim_state",
    "context_strategy",
    "sensitivity",
    "classification_status",
    "data_designation",
)


def validate_window(window: ReportWindow) -> None:
    if window.to_date < window.from_date:
        raise ValidationFailedError(
            "The report window ends before it starts.", remediation="Swap from and to."
        )
    if (window.to_date - window.from_date).days >= MAX_WINDOW_DAYS:
        raise ValidationFailedError(
            f"Report windows are limited to {MAX_WINDOW_DAYS} days.", remediation="Choose a shorter period."
        )


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode()


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        offset = json.loads(base64.urlsafe_b64decode(cursor.encode()))["o"]
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise ValidationFailedError(
            "The page cursor is invalid.", remediation="Start from the first page."
        ) from exc
    if not isinstance(offset, int) or offset < 0:
        raise ValidationFailedError("The page cursor is invalid.", remediation="Start from the first page.")
    return offset


def window_start(window: ReportWindow) -> datetime:
    return datetime.combine(window.from_date, time.min, tzinfo=UTC)


def window_end(window: ReportWindow) -> datetime:
    return datetime.combine(window.to_date + timedelta(days=1), time.min, tzinfo=UTC)


@dataclass(frozen=True)
class ReportContext:
    """One report request: who asks (scope), for which window and filters, with the reference catalog."""

    scope: ReportScope
    window: ReportWindow
    filters: ReportFilters
    catalog: Catalog
    pricing_snapshot: str

    @property
    def hourly_trend(self) -> bool:
        return (self.window.to_date - self.window.from_date).days < HOURLY_TREND_DAYS

    def active_filters(self) -> dict[str, str]:
        return {k: v for k, v in self.filters.model_dump().items() if v is not None}

    def where(self, table: Any) -> list[ColumnElement[bool]]:
        clauses = [self.scope.clause(table)]
        if table is db.ReportAggregateDailyRow:
            clauses += [table.day >= self.window.from_date, table.day <= self.window.to_date]
        else:
            clauses += [
                table.bucket_start >= window_start(self.window),
                table.bucket_start < window_end(self.window),
            ]
        for name, value in self.active_filters().items():
            clauses.append(getattr(table, FILTER_COLUMNS[name]) == value)
        return clauses

    def buckets(self) -> list[datetime]:
        step = timedelta(hours=1) if self.hourly_trend else timedelta(days=1)
        current, end, result = window_start(self.window), window_end(self.window), []
        while current < end:
            result.append(current)
            current += step
        return result

    # --- Labels ------------------------------------------------------------------------------------------

    def label(self, column: str, value: str) -> str:
        catalog = self.catalog
        if column == "application_id":
            app = catalog.application(value)
            return app.name if app else value
        if column == "department_id":
            dept = next((d for d in catalog.tenant.departments if d.id == value), None)
            return dept.name if dept else value
        if column == "model_id":
            model = catalog.model(value)
            return model.display_name if model else (value or "No model call")
        if column == "provider":
            return value or "No provider call"
        if column == "principal" and value == UNATTRIBUTED:
            return "Unattributed user"
        if column == "principal" and value.startswith(SERVICE_PREFIX):
            app = catalog.application(value.removeprefix(SERVICE_PREFIX))
            return f"Service principal · {app.name if app else value}"
        return value


Rows = list[dict[str, Any]]


async def grouped(
    session: AsyncSession,
    ctx: ReportContext,
    *,
    group: str | None,
    extra: Sequence[str] = (),
    by_bucket: bool = False,
) -> Rows:
    """Sums of every measure, grouped by `group`, the base dimensions, `extra` and optionally the bucket."""
    hourly = by_bucket and ctx.hourly_trend
    table: Any = db.ReportAggregateHourlyRow if hourly else db.ReportAggregateDailyRow
    columns: list[Any] = [(getattr(table, group) if group else literal("all")).label("grp")]
    if by_bucket:
        columns.append((table.bucket_start if hourly else table.day).label("bucket"))
    dims = [getattr(table, name) for name in dict.fromkeys((*_BASE, *extra))]
    sums = [func.sum(getattr(table, name)).label(name) for name in MEASURES]
    statement = select(*columns, *dims, *sums).where(*ctx.where(table)).group_by(*columns, *dims)
    rows = []
    for row in await session.execute(statement):
        values = dict(row._mapping)
        if by_bucket and not hourly:
            values["bucket"] = datetime.combine(values["bucket"], time.min, tzinfo=UTC)
        rows.append(values)
    return rows


def by_group(rows: Iterable[dict[str, Any]]) -> dict[str, Totals]:
    groups: dict[str, Totals] = defaultdict(Totals)
    for row in rows:
        groups[str(row["grp"])].add(row)
    return groups


async def metadata(session: AsyncSession, ctx: ReportContext, totals: Totals) -> ReportMetadata:
    table = db.ReportAggregateDailyRow
    versions = (
        await session.execute(
            select(
                func.array_agg(func.distinct(table.taxonomy_version)),
                func.array_agg(func.distinct(table.classifier_version)),
                func.array_agg(func.distinct(table.rate_card_version)),
            ).where(*ctx.where(table), table.line_kind != "run")
        )
    ).one()
    run_versions = (
        await session.execute(
            select(
                func.array_agg(func.distinct(table.taxonomy_version)),
                func.array_agg(func.distinct(table.classifier_version)),
            ).where(*ctx.where(table), table.line_kind == "run")
        )
    ).one()
    refresh = db.ReportRefreshRow
    as_of = await session.scalar(
        select(func.max(refresh.started_at)).where(
            refresh.tenant_id == ctx.scope.tenant_id, refresh.status == "completed"
        )
    )

    def clean(*values: Iterable[str] | None) -> list[str]:
        merged = {v for group in values if group for v in group}
        return sorted(v for v in merged if v and v not in ("none", "not-reached"))

    designation = designation_of(totals.designations)
    return ReportMetadata(
        window=ctx.window,
        filters=ctx.active_filters(),
        scope=ctx.scope.contract(),
        generated_at=datetime.now(UTC),
        aggregates_as_of=as_of,
        granularity="hour" if ctx.hourly_trend else "day",
        taxonomy_versions=clean(run_versions[0]),
        classifier_versions=clean(run_versions[1]),
        rate_card_versions=clean(versions[2]),
        pricing_snapshot=ctx.pricing_snapshot,
        data_designation=designation,
    )


def designation_of(values: set[str]) -> Literal["synthetic", "actual", "mixed", "none"]:
    if not values:
        return "none"
    if values == {"synthetic"}:
        return "synthetic"
    if values == {"actual"}:
        return "actual"
    return "mixed"


def _page(items: list[Any], cursor: str | None, limit: int) -> tuple[list[Any], ReportPage]:
    offset = decode_cursor(cursor)
    page = items[offset : offset + limit]
    following = offset + limit
    return page, ReportPage(
        next_cursor=encode_cursor(following) if following < len(items) else None,
        limit=limit,
        total_rows=len(items),
    )


# --- Filter options --------------------------------------------------------------------------------


async def filter_options(session: AsyncSession, ctx: ReportContext) -> ReportFilterOptions:
    """Distinct values of each filter in the window and scope (other filters ignored: all stay offered)."""
    table = db.ReportAggregateDailyRow
    unfiltered = replace(ctx, filters=ReportFilters())
    columns = [func.array_agg(func.distinct(getattr(table, column))) for column in FILTER_COLUMNS.values()]
    row = (await session.execute(select(*columns).where(*unfiltered.where(table)))).one()
    options = {}
    for (name, column), values in zip(FILTER_COLUMNS.items(), row, strict=True):
        present = sorted(
            v for v in (values or []) if v and (column != "principal" or ctx.scope.users_visible)
        )
        options[name] = [FilterOption(value=v, label=ctx.label(column, v)) for v in present]
    return ReportFilterOptions(window=ctx.window, scope=ctx.scope.contract(), options=options)


# --- Overview ----------------------------------------------------------------------------------------


async def overview(session: AsyncSession, ctx: ReportContext) -> OverviewReport:
    totals = Totals().extend(await grouped(session, ctx, group=None))
    trend_rows = await grouped(session, ctx, group=None, by_bucket=True)
    per_bucket: dict[datetime, Totals] = defaultdict(Totals)
    for row in trend_rows:
        per_bucket[row["bucket"]].add(row)
    frontier = ctx.catalog.models.frontier_baseline
    return OverviewReport(
        metadata=await metadata(session, ctx, totals),
        usage=totals.usage(),
        outcomes=totals.outcome_counts(),
        flow=totals.traffic_flow(),
        packing=totals.packing(),
        cache_hit_rate=ratio(totals.m("cache_hit_count"), totals.requests),
        quality_pass_rate=ratio(totals.m("quality_passed_count"), totals.m("quality_checked_count")),
        cost_variance=totals.cost_variance(),
        savings=totals.savings(frontier, ctx.pricing_snapshot),
        baseline=totals.baseline(frontier, ctx.label("model_id", frontier)),
        value_kpis=ValueKpis(
            cost=totals.cost_kpi(),
            tokens=totals.tokens_kpi(),
            quality=await _quality_kpi(session, ctx, totals),
            governance=totals.governance_kpi(),
        ),
        trend=[_trend_point(bucket, per_bucket.get(bucket, Totals())) for bucket in ctx.buckets()],
    )


QUALITY_COMPARE = "quality.compare"  # the comparison's audit action (assurance.quality_check.AUDIT_ACTION)
_CALLED_NOTHING = ("denied", "not_reserved")


async def _quality_kpi(session: AsyncSession, ctx: ReportContext, totals: Totals) -> QualityKpi:
    """The /live quality counter (web `live/quality.ts`) over the window's runs in scope: each run's latest
    comparison that called the reference, its score when the grader gave one. Read from the comparisons'
    audit rows at request time, like the quality report: never their text, which is never stored."""
    run, audit = db.ExecutionRunRow, db.AuditLogRow
    statement, dims = run_dimension_columns(ctx.catalog.taxonomy.review_confidence_threshold)
    rows = await session.execute(
        statement.join(
            audit,
            (audit.tenant_id == run.tenant_id)
            & (audit.action == QUALITY_COMPARE)
            & (audit.target_type == "run")
            & (audit.target_id == run.run_id),
        )
        .add_columns(audit.details)
        .where(*run_where(ctx, dims))
        .order_by(audit.id)
    )
    comparisons = ((row[0].run_id, row[0].data_designation, row[-1] or {}) for row in rows)
    return quality_kpi(comparisons, totals.quality_comparison_cost())


def quality_kpi(comparisons: Iterable[tuple[str, str, dict[str, Any]]], cost: Amount) -> QualityKpi:
    """Fold (run, data designation, audit details) in audit order into the /live quality counter: one entry
    per run, its latest comparison that called something; a score only when the grader gave one."""
    latest: dict[str, tuple[str, float | None]] = {}
    for run_id, designation, details in comparisons:
        if details.get("status") in _CALLED_NOTHING:
            continue  # a denied or unreserved request never replaces a comparison
        score = details.get("score") if details.get("status") == "compared" else None
        latest[run_id] = (designation, float(score) if score is not None else None)
    scores = [score for _, score in latest.values() if score is not None]
    synthetic = Provenance.SYNTHETIC.value in {designation for designation, _ in latest.values()}
    return QualityKpi(
        compared_requests=len(scores),
        mean_score=sum(scores) / len(scores) if scores else None,
        cost=cost,
        provenance=Provenance.SYNTHETIC if synthetic else Provenance.ESTIMATED,
    )


def _trend_point(bucket: datetime, totals: Totals) -> TrendPoint:
    tokens = totals.tokens()
    return TrendPoint(
        bucket=bucket,
        request_count=totals.requests,
        input_tokens=tokens.input_tokens.total,
        output_tokens=tokens.output_tokens.total,
        cim_tokens=tokens.cim_input_tokens + tokens.cim_output_tokens,
        retry_tokens=tokens.retry_input_tokens + tokens.retry_output_tokens,
        baseline_tokens=tokens.baseline_input_tokens + tokens.baseline_output_tokens,
        quality_comparison_tokens=tokens.quality_comparison_input_tokens
        + tokens.quality_comparison_output_tokens,
        api_micros=sum(totals.api.values()),
        gpu_estimated_micros=totals.m("gpu_estimated_micros"),
        avoided_micros=totals.m("avoided_micros"),
    )


# --- Consumption -------------------------------------------------------------------------------------


def _spend_key(item: tuple[str, Totals]) -> tuple[int, int, int, str]:
    group, totals = item
    tokens = sum(totals.tokens_in.values()) + sum(totals.tokens_out.values())
    return (-sum(totals.api.values()), -tokens, -totals.requests, group)


async def consumption(
    session: AsyncSession, ctx: ReportContext, group_by: str, *, cursor: str | None, limit: int
) -> ConsumptionReport:
    column = CONSUMPTION_GROUPS[group_by]
    rows = await grouped(session, ctx, group=column)
    groups = by_group(rows)
    overall = Totals().extend(rows)
    ranked = sorted(groups.items(), key=_spend_key)
    page, info = _page(ranked, cursor, limit)
    total_api = sum(overall.api.values())
    on_page = {group for group, _ in page}
    series_rows = await grouped(session, ctx, group=column, by_bucket=True)
    series: dict[tuple[datetime, str], Totals] = defaultdict(Totals)
    for row in series_rows:
        if str(row["grp"]) in on_page:
            series[(row["bucket"], str(row["grp"]))].add(row)
    series_groups = [group for group, _ in page][:MAX_SERIES_GROUPS]
    return ConsumptionReport(
        metadata=await metadata(session, ctx, overall),
        group_by=group_by,
        rows=[
            ConsumptionRow(
                group=group,
                label=ctx.label(column, group),
                usage=totals.usage(),
                spend_share=ratio(sum(totals.api.values()), total_api),
            )
            for group, totals in page
        ],
        totals=overall.usage(),
        series=[
            _series_point(bucket, group, series.get((bucket, group), Totals()))
            for bucket in ctx.buckets()
            for group in series_groups
        ],
        page=info,
    )


def _series_point(bucket: datetime, group: str, totals: Totals) -> SeriesPoint:
    point = _trend_point(bucket, totals)
    return SeriesPoint(
        bucket=bucket,
        group=group,
        request_count=point.request_count,
        input_tokens=point.input_tokens,
        output_tokens=point.output_tokens,
        cim_tokens=point.cim_tokens,
        retry_tokens=point.retry_tokens,
        api_micros=point.api_micros,
    )


# --- Classification ----------------------------------------------------------------------------------


async def classification(session: AsyncSession, ctx: ReportContext, group_by: str) -> ClassificationReport:
    column = CLASSIFICATION_GROUPS[group_by]
    rows = await grouped(session, ctx, group=column, extra=("model_id",))
    groups = by_group(rows)
    overall = Totals().extend(rows)
    total = overall.requests
    ranked = sorted(groups.items(), key=lambda item: (-item[1].requests, item[0]))
    trend_rows = await grouped(session, ctx, group=column, by_bucket=True)
    trend: Counter[tuple[datetime, str]] = Counter()
    for row in trend_rows:
        if row["line_kind"] == "run":
            trend[(row["bucket"], str(row["grp"]))] += int(row["request_count"] or 0)
    taxonomy = ctx.catalog.taxonomy
    return ClassificationReport(
        metadata=await metadata(session, ctx, overall),
        group_by=group_by,
        rows=[
            ClassificationRow(
                group=group,
                label=group,
                request_count=totals.requests,
                share=ratio(totals.requests, total),
                tokens=sum(totals.tokens_in.values()) + sum(totals.tokens_out.values()),
                api=totals.api_amount(),
                low_confidence=group == UNCLASSIFIED,
                models=[
                    ModelSuitability(
                        model_id=model_id,
                        request_count=counts["requests"],
                        quality_checked=counts["checked"],
                        quality_passed=counts["passed"],
                    )
                    for model_id, counts in sorted(totals.served_by.items(), key=lambda m: -m[1]["requests"])
                    if model_id
                ],
            )
            for group, totals in ranked
            if totals.requests or totals.api
        ],
        trend=[
            ClassificationTrendPoint(bucket=bucket, group=group, request_count=count)
            for (bucket, group), count in sorted(trend.items())
        ],
        unclassified=ratio(overall.statuses.get(UNCLASSIFIED, 0), total),
        taxonomy=TaxonomyInfo(
            version=taxonomy.version, review_confidence_threshold=taxonomy.review_confidence_threshold
        ),
    )


# --- Optimization ------------------------------------------------------------------------------------


async def optimization(session: AsyncSession, ctx: ReportContext, group_by: str) -> OptimizationReport:
    column = OPTIMIZATION_GROUPS[group_by]
    rows = await grouped(session, ctx, group=column)
    groups = by_group(rows)
    overall = Totals().extend(rows)
    frontier = ctx.catalog.models.frontier_baseline
    ranked = sorted(groups.items(), key=_spend_key)
    return OptimizationReport(
        metadata=await metadata(session, ctx, overall),
        group_by=group_by,
        rows=[_optimization_row(ctx, column, group, totals, frontier) for group, totals in ranked],
        packing=overall.packing(),
        savings=overall.savings(frontier, ctx.pricing_snapshot),
        baseline=overall.baseline(frontier, ctx.label("model_id", frontier)),
    )


def _optimization_row(
    ctx: ReportContext, column: str, group: str, totals: Totals, frontier: str
) -> OptimizationRow:
    before, after = totals.m("context_tokens_before"), totals.m("context_tokens_after")
    cim_labels = totals.stage_labels.get("cim", set())
    attempts = totals.m("attempt_count")
    latency_samples = totals.m("attempt_latency_count")
    return OptimizationRow(
        group=group,
        label=ctx.label(column, group),
        request_count=totals.requests,
        cim_invoked=totals.cim_states["invoked"],
        cim_bypassed_by_policy=totals.cim_states["bypassed-policy"],
        cim_skipped=totals.cim_states["skipped-sufficient"],
        local_fast=totals.paths["local-fast"],
        sovereign_direct=totals.paths["sovereign-direct"],
        cache_hits=totals.m("cache_hit_count"),
        context_tokens_before=before,
        context_tokens_after=after,
        context_reduction=ratio(before - after, before),
        packing=totals.packing(),
        cached_input_tokens=totals.m("cached_input_tokens"),
        avoided=totals.avoided(),
        cim_overhead=CimOverhead(
            input_tokens=totals.stage_in["cim"],
            output_tokens=totals.stage_out["cim"],
            api=_amount(totals, totals.stage_api["cim"], cim_labels),
            gpu_estimated=_amount(
                totals, totals.stage_gpu["cim"], cim_labels, floor=Provenance.ESTIMATED.value
            ),
        ),
        retries=RetrySummary(
            retries=totals.m("retry_count"),
            failed_attempts=totals.m("failed_attempt_count"),
            attempts=attempts,
            tokens=totals.m("retry_input_tokens") + totals.m("retry_output_tokens"),
            api=_amount(totals, totals.m("retry_micros"), totals.usage_labels),
        ),
        latency=LatencySummary(
            average_attempt_ms=totals.measures["attempt_latency_ms_sum"] / latency_samples
            if latency_samples
            else None,
            average_run_ms=totals.measures["run_latency_ms_sum"] / totals.requests
            if totals.requests
            else None,
            attempt_samples=latency_samples,
        ),
        savings=totals.savings(frontier, ctx.pricing_snapshot),
    )


def _amount(totals: Totals, value: int, labels: set[str], *, floor: str | None = None) -> Amount:
    return Amount(value=value, provenance=totals.label(labels, floor=floor))


# --- Governance --------------------------------------------------------------------------------------


async def governance(session: AsyncSession, ctx: ReportContext, group_by: str) -> GovernanceReport:
    column = GOVERNANCE_GROUPS[group_by]
    rows = await grouped(session, ctx, group=column, extra=("deny_rule_id",))
    groups = by_group(rows)
    overall = Totals().extend(rows)
    ranked = sorted(groups.items(), key=lambda item: (-item[1].requests, item[0]))
    return GovernanceReport(
        metadata=await metadata(session, ctx, overall),
        group_by=group_by,
        rows=[_governance_row(ctx, column, group, totals) for group, totals in ranked],
        totals=_governance_row(ctx, column, "all", overall, label="All requests"),
        alerts=await _alert_counts(session, ctx),
        policy_versions=await _policy_versions(session, ctx),
    )


def _governance_row(
    ctx: ReportContext, column: str, group: str, totals: Totals, *, label: str | None = None
) -> GovernanceRow:
    outcomes = totals.outcomes
    denied = outcomes["policy_denied"] + outcomes["approval_required"] + outcomes["quota_denied"]
    return GovernanceRow(
        group=group,
        label=label or ctx.label(column, group),
        request_count=totals.requests,
        allowed=totals.requests - denied,
        policy_denied=outcomes["policy_denied"],
        approval_required=outcomes["approval_required"],
        quota_denied=outcomes["quota_denied"],
        failed=outcomes["timeout"] + outcomes["error"],
        confidential_requests=totals.confidential_requests,
        sovereign_direct=totals.paths["sovereign-direct"],
        cim_bypassed_by_policy=totals.cim_states["bypassed-policy"],
        confidential_external_attempts=totals.m("confidential_external_attempts"),
        confidential_cim_attempts=totals.m("confidential_cim_attempts"),
        confidential_comparison_attempts=totals.m("confidential_comparison_attempts"),
        approval_exceptions=totals.m("approval_exception_count"),
        denial_reasons=[
            RuleCount(rule_id=rule, count=count) for rule, count in totals.deny_rules.most_common() if count
        ],
    )


def alert_scope(scope: ReportScope) -> ColumnElement[bool]:
    """Alerts a reporting scope may see: tenant-wide, or the scope's applications and department (the same
    row-level rule as the FinOps alerts API)."""
    tenant: ColumnElement[bool] = (db.BudgetAlertRow.tenant_id == scope.tenant_id) & admin_alerts()
    if scope.tenant_wide:
        return tenant
    return tenant & alert_visibility(scope.application_ids, scope.department_id)


async def _alert_counts(session: AsyncSession, ctx: ReportContext) -> list[AlertCount]:
    alert = db.BudgetAlertRow
    rows = await session.execute(
        select(alert.kind, func.count())
        .where(
            alert_scope(ctx.scope),
            alert.created_at >= window_start(ctx.window),
            alert.created_at < window_end(ctx.window),
        )
        .group_by(alert.kind)
        .order_by(alert.kind)
    )
    return [AlertCount(kind=kind, count=int(count)) for kind, count in rows]


async def _policy_versions(session: AsyncSession, ctx: ReportContext) -> list[PolicyVersionChange]:
    version = db.PolicyVersionRow
    rows = await session.execute(
        select(version.version, version.published_at)
        .where(
            version.tenant_id == ctx.scope.tenant_id,
            version.published_at >= window_start(ctx.window),
            version.published_at < window_end(ctx.window),
        )
        .order_by(version.published_at)
    )
    return [PolicyVersionChange(version=str(v), published_at=at) for v, at in rows if at is not None]


# --- Run list (drilldown to the trace) ----------------------------------------------------------------


def run_where(ctx: ReportContext, dims: dict[str, Any]) -> list[ColumnElement[bool]]:
    """Scope, window and filters on raw run rows (with `run_dimension_columns`): a run matches a model or
    provider filter when any of its attempts does."""
    run, attempt = db.ExecutionRunRow, db.ModelAttemptRow
    where: list[ColumnElement[bool]] = [
        ctx.scope.clause(run),
        run.created_at >= window_start(ctx.window),
        run.created_at < window_end(ctx.window),
    ]
    for name, value in ctx.active_filters().items():
        column = FILTER_COLUMNS[name]
        if column in ("model_id", "provider"):
            target = attempt.model_id if column == "model_id" else attempt.provider
            where.append(exists().where(attempt.run_id == run.run_id, target == value))
        else:
            where.append(dims[column] == value)
    return where


async def run_list(
    session: AsyncSession, ctx: ReportContext, threshold: float, *, cursor: str | None, limit: int
) -> RunList:
    run, attempt, ledger = db.ExecutionRunRow, db.ModelAttemptRow, db.UsageLedgerRow
    statement, dims = run_dimension_columns(threshold)
    where = run_where(ctx, dims)
    served = (
        select(attempt.model_id)
        .where(attempt.run_id == run.run_id, attempt.stage == "upstream", attempt.status == "ok")
        .order_by(attempt.sequence.desc())
        .limit(1)
        .scalar_subquery()
    )
    tokens_in = select(func.coalesce(func.sum(attempt.input_tokens), 0)).where(attempt.run_id == run.run_id)
    tokens_out = select(func.coalesce(func.sum(attempt.output_tokens), 0)).where(attempt.run_id == run.run_id)
    api = select(func.coalesce(func.sum(ledger.amount_micros), 0)).where(
        ledger.run_id == run.run_id, ledger.charge_type != "gpu_estimate"
    )
    count = await session.scalar(select(func.count()).select_from(statement.where(*where).subquery()))
    offset = decode_cursor(cursor)
    rows = await session.execute(
        statement.add_columns(
            dims["department_id"].label("department_label"),
            dims["principal"].label("principal_label"),
            dims["task_category"].label("task_label"),
            served.label("served_model"),
            tokens_in.scalar_subquery().label("input_tokens"),
            tokens_out.scalar_subquery().label("output_tokens"),
            api.scalar_subquery().label("api_micros"),
        )
        .where(*where)
        .order_by(run.created_at.desc(), run.run_id.desc())
        .offset(offset)
        .limit(limit)
    )
    runs = []
    for row in rows:
        record: db.ExecutionRunRow = row[0]
        runs.append(
            RunSummary(
                run_id=record.run_id,
                trace_id=record.trace_id,
                created_at=record.created_at,
                application_id=record.application_id,
                department_id=str(row.department_label),
                principal=shown_principal(ctx.scope, str(row.principal_label)),
                task_category=str(row.task_label),
                sensitivity=record.effective_classification,
                execution_path=record.execution_path,
                outcome=record.outcome,
                model_id=row.served_model,
                input_tokens=int(row.input_tokens),
                output_tokens=int(row.output_tokens),
                api_micros=int(row.api_micros),
                provenance=run_label(record.provenance, record.data_designation),
                data_designation=record.data_designation,
            )
        )
    total = int(count or 0)
    following = offset + limit
    matching = statement.where(*where).subquery()
    designations = await session.scalars(select(matching.c.data_designation).distinct())
    totals = Totals(designations=set(designations))
    return RunList(
        metadata=await metadata(session, ctx, totals),
        runs=runs,
        page=ReportPage(
            next_cursor=encode_cursor(following) if following < total else None, limit=limit, total_rows=total
        ),
    )
