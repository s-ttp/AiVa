"""AiVa Insights reporting APIs (LLD §21.6; ADR-0015).

Every endpoint requires `reports:read` (exports `reports:export`, identity resolution `reports:identity`)
and applies the caller's row-level scope in the query layer (LLD §21.8): tenant-wide for administrators,
finance analysts and security auditors, the department for department managers, owned applications for
application owners. Users are pseudonymous throughout.
"""

from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import select

from aiva_gateway.api.deps import CallerDep, ServicesDep, SessionDep
from aiva_gateway.api.errors import ERROR_RESPONSES
from aiva_gateway.auth.principal import Caller
from aiva_gateway.auth.rbac import Permission, require
from aiva_gateway.contracts.common import ErrorEnvelope
from aiva_gateway.contracts.reports import (
    ClassificationGroup,
    ClassificationReport,
    ConsumptionGroup,
    ConsumptionReport,
    ExportJob,
    ExportList,
    ExportRequest,
    GovernanceGroup,
    GovernanceReport,
    IdentityResolution,
    IdentityResolutionRequest,
    OptimizationGroup,
    OptimizationReport,
    OverviewReport,
    QualityGroup,
    QualityReport,
    ReportFilterOptions,
    ReportFilters,
    ReportRunDetail,
    ReportWindow,
    RunList,
)
from aiva_gateway.db.models import ReportExportRow
from aiva_gateway.reporting import exports, queries
from aiva_gateway.reporting.aggregate import build_pricing
from aiva_gateway.reporting.detail import run_detail
from aiva_gateway.reporting.identity import resolve_identities
from aiva_gateway.reporting.quality import quality as quality_report
from aiva_gateway.reporting.scope import check_filters, check_group, pseudonym_of, report_scope
from aiva_gateway.services import Services

router = APIRouter(tags=["reports"])

_NOT_FOUND = {**ERROR_RESPONSES, 404: {"model": ErrorEnvelope, "description": "Not found or out of scope."}}

FromDate = Annotated[date, Query(alias="from", description="First UTC day, inclusive.")]
ToDate = Annotated[date, Query(alias="to", description="Last UTC day, inclusive.")]
TenantId = Annotated[str | None, Query(max_length=64, description="Defaults to (and must be) your tenant.")]
Cursor = Annotated[str | None, Query(max_length=200)]
Limit = Annotated[int, Query(ge=1, le=500)]


def report_filters(
    department_id: Annotated[str | None, Query(max_length=64)] = None,
    application_id: Annotated[str | None, Query(max_length=64)] = None,
    user: Annotated[str | None, Query(max_length=80, description="Pseudonym or service:<app>.")] = None,
    model_id: Annotated[str | None, Query(max_length=64)] = None,
    provider: Annotated[str | None, Query(max_length=32)] = None,
    business_function: Annotated[str | None, Query(max_length=32)] = None,
    task_category: Annotated[str | None, Query(max_length=32)] = None,
    intent: Annotated[str | None, Query(max_length=64)] = None,
    complexity: Annotated[str | None, Query(max_length=32)] = None,
    sensitivity: Annotated[str | None, Query(max_length=32)] = None,
    language: Annotated[str | None, Query(max_length=16)] = None,
    execution_path: Annotated[str | None, Query(max_length=32)] = None,
    source_policy: Annotated[str | None, Query(max_length=64)] = None,
    environment: Annotated[str | None, Query(max_length=32)] = None,
    outcome: Annotated[str | None, Query(max_length=32)] = None,
    data_designation: Annotated[str | None, Query(max_length=16)] = None,
) -> ReportFilters:
    return ReportFilters(
        department_id=department_id,
        application_id=application_id,
        user=user,
        model_id=model_id,
        provider=provider,
        business_function=business_function,
        task_category=task_category,
        intent=intent,
        complexity=complexity,
        sensitivity=sensitivity,
        language=language,
        execution_path=execution_path,
        source_policy=source_policy,
        environment=environment,
        outcome=outcome,
        data_designation=data_designation,
    )


FiltersDep = Annotated[ReportFilters, Depends(report_filters)]


def _context(
    caller: Caller,
    services: Services,
    from_date: date,
    to_date: date,
    filters: ReportFilters,
    tenant_id: str | None,
) -> queries.ReportContext:
    require(caller, Permission.REPORTS_READ)
    scope = report_scope(caller, services.catalog, tenant_id)
    window = ReportWindow.model_validate({"from": from_date, "to": to_date})
    queries.validate_window(window)
    check_filters(scope, filters, services.catalog)
    return queries.ReportContext(
        scope=scope,
        window=window,
        filters=filters,
        catalog=services.catalog,
        pricing_snapshot=services.prices.version,
    )


@router.get(
    "/v1/reports/filters",
    response_model=ReportFilterOptions,
    responses=ERROR_RESPONSES,
    summary="Values each global filter can take in the window, within your reporting scope",
)
async def filter_values(
    caller: CallerDep,
    services: ServicesDep,
    session: SessionDep,
    from_date: FromDate,
    to_date: ToDate,
    tenant_id: TenantId = None,
) -> ReportFilterOptions:
    ctx = _context(caller, services, from_date, to_date, ReportFilters(), tenant_id)
    return await queries.filter_options(session, ctx)


@router.get(
    "/v1/reports/overview",
    response_model=OverviewReport,
    responses=ERROR_RESPONSES,
    summary="KPI totals, spend, tokens, cache rate, quality pass rate, cost variance, savings and trend",
)
async def overview(
    caller: CallerDep,
    services: ServicesDep,
    session: SessionDep,
    from_date: FromDate,
    to_date: ToDate,
    filters: FiltersDep,
    tenant_id: TenantId = None,
) -> OverviewReport:
    ctx = _context(caller, services, from_date, to_date, filters, tenant_id)
    return await queries.overview(session, ctx)


@router.get(
    "/v1/reports/consumption",
    response_model=ConsumptionReport,
    responses=ERROR_RESPONSES,
    summary="Consumption grouped by application, department, user, category, model, provider or path",
)
async def consumption(
    caller: CallerDep,
    services: ServicesDep,
    session: SessionDep,
    group_by: ConsumptionGroup,
    from_date: FromDate,
    to_date: ToDate,
    filters: FiltersDep,
    tenant_id: TenantId = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> ConsumptionReport:
    ctx = _context(caller, services, from_date, to_date, filters, tenant_id)
    check_group(ctx.scope, group_by)
    return await queries.consumption(session, ctx, group_by, cursor=cursor, limit=limit)


@router.get(
    "/v1/reports/classification",
    response_model=ClassificationReport,
    responses=ERROR_RESPONSES,
    summary="Workload distribution and trend by a classification dimension; low confidence is unclassified",
)
async def classification(
    caller: CallerDep,
    services: ServicesDep,
    session: SessionDep,
    group_by: ClassificationGroup,
    from_date: FromDate,
    to_date: ToDate,
    filters: FiltersDep,
    tenant_id: TenantId = None,
) -> ClassificationReport:
    ctx = _context(caller, services, from_date, to_date, filters, tenant_id)
    return await queries.classification(session, ctx, group_by)


@router.get(
    "/v1/reports/optimization",
    response_model=OptimizationReport,
    responses=ERROR_RESPONSES,
    summary="Context reduction, cache effects, savings and overhead by application, strategy or model",
)
async def optimization(
    caller: CallerDep,
    services: ServicesDep,
    session: SessionDep,
    group_by: OptimizationGroup,
    from_date: FromDate,
    to_date: ToDate,
    filters: FiltersDep,
    tenant_id: TenantId = None,
) -> OptimizationReport:
    ctx = _context(caller, services, from_date, to_date, filters, tenant_id)
    return await queries.optimization(session, ctx, group_by)


@router.get(
    "/v1/reports/governance",
    response_model=GovernanceReport,
    responses=ERROR_RESPONSES,
    summary="Denials, quota events, confidential routing and exceptions by policy, application or source",
)
async def governance(
    caller: CallerDep,
    services: ServicesDep,
    session: SessionDep,
    group_by: GovernanceGroup,
    from_date: FromDate,
    to_date: ToDate,
    filters: FiltersDep,
    tenant_id: TenantId = None,
) -> GovernanceReport:
    ctx = _context(caller, services, from_date, to_date, filters, tenant_id)
    return await queries.governance(session, ctx, group_by)


@router.get(
    "/v1/reports/quality",
    response_model=QualityReport,
    responses=ERROR_RESPONSES,
    summary="Output assurance, CIM sufficiency, Context Guard verdicts and answer-citation coverage",
)
async def quality(
    caller: CallerDep,
    services: ServicesDep,
    session: SessionDep,
    from_date: FromDate,
    to_date: ToDate,
    filters: FiltersDep,
    group_by: QualityGroup = "application",
    tenant_id: TenantId = None,
) -> QualityReport:
    ctx = _context(caller, services, from_date, to_date, filters, tenant_id)
    threshold = services.catalog.taxonomy.review_confidence_threshold
    return await quality_report(session, ctx, group_by, threshold)


@router.get(
    "/v1/reports/runs",
    response_model=RunList,
    responses=ERROR_RESPONSES,
    summary="Runs matching the report filters, newest first: the last drilldown level before the trace",
)
async def runs(
    caller: CallerDep,
    services: ServicesDep,
    session: SessionDep,
    from_date: FromDate,
    to_date: ToDate,
    filters: FiltersDep,
    tenant_id: TenantId = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> RunList:
    ctx = _context(caller, services, from_date, to_date, filters, tenant_id)
    threshold = services.catalog.taxonomy.review_confidence_threshold
    return await queries.run_list(session, ctx, threshold, cursor=cursor, limit=limit)


@router.get(
    "/v1/reports/runs/{trace_id}",
    response_model=ReportRunDetail,
    responses=_NOT_FOUND,
    summary="Authorized request drilldown: attempt-level usage, classification, policy and validation",
)
async def run_report(
    trace_id: str,
    caller: CallerDep,
    services: ServicesDep,
    session: SessionDep,
) -> ReportRunDetail:
    require(caller, Permission.REPORTS_READ)
    scope = report_scope(caller, services.catalog)
    return await run_detail(
        session,
        scope,
        trace_id,
        threshold=services.catalog.taxonomy.review_confidence_threshold,
        pricing=build_pricing(services.catalog, services.prices),
    )


@router.post(
    "/v1/reports/identities/resolve",
    response_model=IdentityResolution,
    responses=ERROR_RESPONSES,
    summary="Resolve pseudonyms to identities (separately permissioned; every resolution is audited)",
)
async def resolve(
    body: IdentityResolutionRequest, caller: CallerDep, services: ServicesDep, session: SessionDep
) -> IdentityResolution:
    require(caller, Permission.REPORTS_IDENTITY)
    result = await resolve_identities(session, services.settings, services.catalog, caller, body)
    await session.commit()
    return result


@router.post(
    "/v1/reports/exports",
    response_model=ExportJob,
    status_code=202,
    responses=ERROR_RESPONSES,
    summary="Request an asynchronous CSV export (RBAC-enforced, time-limited download, audited)",
)
async def create_export(
    body: ExportRequest, caller: CallerDep, services: ServicesDep, session: SessionDep
) -> ExportJob:
    require(caller, Permission.REPORTS_EXPORT)
    scope = report_scope(caller, services.catalog)
    row = await exports.create_export(
        session,
        services.catalog,
        scope,
        caller,
        body,
        ttl_seconds=services.settings.report_export_ttl_seconds,
    )
    await session.commit()
    exports.start_export(
        services.sessionmaker, services.catalog, row.export_id, pricing_snapshot=services.prices.version
    )
    return exports.job_of(row, datetime.now(UTC))


@router.get(
    "/v1/reports/exports",
    response_model=ExportList,
    responses=ERROR_RESPONSES,
    summary="Your recent exports",
)
async def list_exports(caller: CallerDep, session: SessionDep) -> ExportList:
    require(caller, Permission.REPORTS_EXPORT)
    rows = await session.scalars(
        select(ReportExportRow)
        .where(
            ReportExportRow.tenant_id == caller.tenant_id,
            ReportExportRow.requester_pseudonym == pseudonym_of(caller),
        )
        .order_by(ReportExportRow.created_at.desc())
        .limit(50)
    )
    now = datetime.now(UTC)
    return ExportList(exports=[exports.job_of(row, now) for row in rows])


@router.get(
    "/v1/reports/exports/{export_id}",
    response_model=ExportJob,
    responses=_NOT_FOUND,
    summary="Export status",
)
async def get_export(export_id: str, caller: CallerDep, session: SessionDep) -> ExportJob:
    require(caller, Permission.REPORTS_EXPORT)
    row = await exports.own_export(session, caller, export_id)
    return exports.job_of(row, datetime.now(UTC))


@router.get(
    "/v1/reports/exports/{export_id}/download",
    response_class=Response,
    responses={
        **_NOT_FOUND,
        200: {"content": {"text/csv": {}}, "description": "The CSV file (metadata lines start with #)."},
        409: {"model": ErrorEnvelope, "description": "The export is not ready."},
        410: {"model": ErrorEnvelope, "description": "The export expired and was deleted."},
    },
    summary="Download your export before it expires (audited)",
)
async def download_export(
    export_id: str, caller: CallerDep, services: ServicesDep, session: SessionDep
) -> Response:
    # Permissions and roles are read afresh on every request, so a revoked reports:export is refused here.
    require(caller, Permission.REPORTS_EXPORT)
    scope = report_scope(caller, services.catalog)
    try:
        row, content = await exports.download(session, caller, export_id, scope=scope)
    finally:
        await session.commit()
    filename = (
        f"aiva-{row.report}-{row.group_by}-{row.window_from.isoformat()}-{row.window_to.isoformat()}.csv"
    )
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )
