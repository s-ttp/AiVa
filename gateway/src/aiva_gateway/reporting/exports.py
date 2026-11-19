"""Asynchronous CSV exports (LLD §21.6, §21.8, §22.6).

`POST /v1/reports/exports` validates the request against the caller's scope, records the job and the
caller's scope snapshot, audits the request and renders the CSV in the background from the same report
queries. Only the requester can download it, only until it expires (the content is then purged), and every
download is audited. Exports carry aggregate figures with pseudonymous users only: never prompt text,
subscriber identifiers, resolved identities or raw PII. Text cells are neutralised against spreadsheet
formula injection.
"""

import asyncio
import csv
import hashlib
import io
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, get_args

from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from aiva_gateway.auth.principal import Caller
from aiva_gateway.catalog import Catalog
from aiva_gateway.contracts.reports import (
    ClassificationReport,
    ConsumptionReport,
    ExportJob,
    ExportRequest,
    GovernanceReport,
    OptimizationReport,
    ProvenanceSplit,
    ReportFilters,
    ReportMetadata,
    ReportWindow,
    SavingsStep,
)
from aiva_gateway.core.errors import AivaError, NotFoundError, PermissionDeniedError, ValidationFailedError
from aiva_gateway.core.ids import new_id
from aiva_gateway.core.money import micros_to_usd
from aiva_gateway.db.models import AuditLogRow, ReportExportRow
from aiva_gateway.reporting import queries
from aiva_gateway.reporting.scope import ReportScope, check_filters, pseudonym_of

logger = logging.getLogger(__name__)

MAX_EXPORT_ROWS = 50_000
# A job still queued or running after this long was interrupted (e.g. the process restarted).
STALE_AFTER = timedelta(minutes=10)
_IN_FLIGHT = ("queued", "running")
INTERRUPTED = "interrupted"
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_PENDING: set["asyncio.Task[None]"] = set()


class ExportExpiredError(AivaError):
    status_code = 410
    code = "EXPORT_EXPIRED"


class ExportNotReadyError(AivaError):
    status_code = 409
    code = "EXPORT_NOT_READY"


def safe_cell(value: Any) -> Any:
    """Neutralise text a spreadsheet would evaluate as a formula (CSV injection)."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return f"'{value}"
    return value


def usd(micros: int) -> str:
    return f"{micros_to_usd(micros):f}"


async def create_export(
    session: AsyncSession,
    catalog: Catalog,
    scope: ReportScope,
    caller: Caller,
    body: ExportRequest,
    *,
    ttl_seconds: int,
) -> ReportExportRow:
    """Validate, record and audit an export request. The caller commits, then starts `run_export`."""
    requester = pseudonym_of(caller)
    groups = queries.GROUPS[body.report]
    if body.group_by not in groups:
        raise ValidationFailedError(
            f"group_by must be one of {', '.join(groups)} for the {body.report} report.",
            details={"errors": [f"group_by: one of {', '.join(groups)}"]},
        )
    queries.validate_window(body.window)
    check_filters(scope, body.filters, catalog)
    now = datetime.now(UTC)
    filters = {k: v for k, v in body.filters.model_dump().items() if v is not None}
    row = ReportExportRow(
        export_id=new_id("exp"),
        tenant_id=scope.tenant_id,
        requester_pseudonym=requester,
        report=body.report,
        group_by=body.group_by,
        window_from=body.window.from_date,
        window_to=body.window.to_date,
        filters=filters,
        scope=scope.snapshot(),
        status="queued",
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    session.add(row)
    session.add(_audit(caller, "report.export.request", row, {"filters": filters}))
    return row


def _audit(caller: Caller, action: str, row: ReportExportRow, extra: dict[str, Any]) -> AuditLogRow:
    return AuditLogRow(
        tenant_id=row.tenant_id,
        actor_kind="user",
        actor_id=pseudonym_of(caller),
        action=action,
        target_type="report_export",
        target_id=row.export_id,
        details={
            "report": row.report,
            "group_by": row.group_by,
            "window": [row.window_from.isoformat(), row.window_to.isoformat()],
            **extra,
        },
    )


def start_export(
    sessionmaker: async_sessionmaker[AsyncSession], catalog: Catalog, export_id: str, *, pricing_snapshot: str
) -> None:
    """Render the CSV in the background of this process (LLD §21.2: a controlled service for the POC)."""
    task = asyncio.create_task(
        run_export(sessionmaker, catalog, export_id, pricing_snapshot=pricing_snapshot),
        name=f"aiva-export-{export_id}",
    )
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


async def run_export(
    sessionmaker: async_sessionmaker[AsyncSession], catalog: Catalog, export_id: str, *, pricing_snapshot: str
) -> None:
    try:
        async with sessionmaker() as session, session.begin():
            row = await session.get(ReportExportRow, export_id, with_for_update=True)
            if row is None or row.status != "queued":
                return
            row.status = "running"
        async with sessionmaker() as session:
            ctx = queries.ReportContext(
                scope=ReportScope.from_snapshot(row.scope),
                window=ReportWindow.model_validate({"from": row.window_from, "to": row.window_to}),
                filters=ReportFilters.model_validate(row.filters),
                catalog=catalog,
                pricing_snapshot=pricing_snapshot,
            )
            content, count = await render(session, ctx, row.report, row.group_by)
        async with sessionmaker() as session, session.begin():
            await session.execute(
                update(ReportExportRow)
                .where(ReportExportRow.export_id == export_id, ReportExportRow.status == "running")
                .values(
                    status="completed",
                    content=content,
                    content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                    row_count=count,
                    completed_at=datetime.now(UTC),
                )
            )
    except (SQLAlchemyError, OSError, ValueError, AivaError) as exc:
        logger.warning("Export %s failed", export_id, exc_info=True)
        try:
            async with sessionmaker() as session, session.begin():
                await session.execute(
                    update(ReportExportRow)
                    .where(ReportExportRow.export_id == export_id, ReportExportRow.status.in_(_IN_FLIGHT))
                    .values(status="failed", error=f"{type(exc).__name__}", completed_at=datetime.now(UTC))
                )
        except SQLAlchemyError:
            logger.warning("Could not record the failure of export %s", export_id, exc_info=True)


async def render(
    session: AsyncSession, ctx: queries.ReportContext, report: str, group_by: str
) -> tuple[str, int]:
    """The CSV text (metadata lines, header, rows) and its row count."""
    header: list[str]
    rows: list[list[Any]]
    if report == "consumption":
        consumption = await queries.consumption(session, ctx, group_by, cursor=None, limit=MAX_EXPORT_ROWS)
        header, rows, meta = *_consumption_table(consumption), consumption.metadata
    elif report == "classification":
        classification = await queries.classification(session, ctx, group_by)
        header, rows, meta = *_classification_table(classification), classification.metadata
    elif report == "optimization":
        optimization = await queries.optimization(session, ctx, group_by)
        header, rows, meta = *_optimization_table(optimization), optimization.metadata
    else:
        governance = await queries.governance(session, ctx, group_by)
        header, rows, meta = *_governance_table(governance), governance.metadata
    buffer = io.StringIO()
    for key, value in _metadata_lines(meta, report, group_by):
        buffer.write(f"# {key}: {value}\n")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for row in rows[:MAX_EXPORT_ROWS]:
        writer.writerow([safe_cell(cell) for cell in row])
    return buffer.getvalue(), min(len(rows), MAX_EXPORT_ROWS)


def _metadata_lines(meta: ReportMetadata, report: str, group_by: str) -> list[tuple[str, str]]:
    filters = ", ".join(f"{k}={v}" for k, v in sorted(meta.filters.items())) or "none"
    scope = meta.scope.level + (f" ({meta.scope.department_id})" if meta.scope.department_id else "")
    return [
        ("AiVa Insights export", f"{report} by {group_by}"),
        ("Data designation", meta.data_designation),
        ("Period", f"{meta.window.from_date.isoformat()} to {meta.window.to_date.isoformat()} (inclusive)"),
        ("Timezone", meta.window.timezone),
        ("Filters", filters),
        ("Reporting scope", scope),
        ("Users", "pseudonymous"),
        ("Generated at", meta.generated_at.isoformat()),
        (
            "Aggregates as of",
            meta.aggregates_as_of.isoformat() if meta.aggregates_as_of else "never refreshed",
        ),
        ("Taxonomy versions", ", ".join(meta.taxonomy_versions) or "none"),
        ("Classifier versions", ", ".join(meta.classifier_versions) or "none"),
        ("Pricing snapshot", meta.pricing_snapshot),
        ("Rate card versions", ", ".join(meta.rate_card_versions) or "none"),
        ("Money", "integer micro-USD (*_micros); *_usd columns are exact decimal renderings"),
        ("Attribution", meta.attribution),
    ]


def _split(split: ProvenanceSplit) -> list[Any]:
    return [split.total, split.measured, split.estimated, split.simulated, split.replayed, split.synthetic]


def _split_header(prefix: str) -> list[str]:
    return [
        f"{prefix}_{part}"
        for part in ("total", "measured", "estimated", "simulated", "replayed", "synthetic")
    ]


def _consumption_table(report: ConsumptionReport) -> tuple[list[str], list[list[Any]]]:
    header = [
        "group",
        "label",
        "requests",
        *_split_header("input_tokens"),
        *_split_header("output_tokens"),
        "upstream_input_tokens",
        "upstream_output_tokens",
        "cim_input_tokens",
        "cim_output_tokens",
        "retry_input_tokens",
        "retry_output_tokens",
        "baseline_input_tokens",
        "baseline_output_tokens",
        "cached_input_tokens",
        *_split_header("api_spend_micros"),
        "api_spend_usd",
        "api_spend_provenance",
        "gpu_estimated_micros",
        "gpu_estimated_provenance",
        "avoided_calls",
        "avoided_tokens",
        "avoided_micros",
        "avoided_provenance",
    ]
    rows = []
    for row in report.rows:
        usage = row.usage
        tokens, spend = usage.tokens, usage.spend
        rows.append(
            [
                row.group,
                row.label,
                usage.request_count,
                *_split(tokens.input_tokens),
                *_split(tokens.output_tokens),
                tokens.upstream_input_tokens,
                tokens.upstream_output_tokens,
                tokens.cim_input_tokens,
                tokens.cim_output_tokens,
                tokens.retry_input_tokens,
                tokens.retry_output_tokens,
                tokens.baseline_input_tokens,
                tokens.baseline_output_tokens,
                tokens.cached_input_tokens,
                *_split(spend.api),
                usd(spend.api.total),
                spend.api.provenance.value,
                spend.gpu_estimated.value,
                spend.gpu_estimated.provenance.value,
                usage.avoided.calls,
                usage.avoided.tokens,
                usage.avoided.micros,
                usage.avoided.provenance.value,
            ]
        )
    return header, rows


def _classification_table(report: ClassificationReport) -> tuple[list[str], list[list[Any]]]:
    header = [
        "group",
        "requests",
        "share",
        "tokens",
        "api_spend_micros",
        "api_spend_provenance",
        "low_confidence",
    ]
    rows = [
        [
            row.group,
            row.request_count,
            f"{row.share.value:.4f}" if row.share.value is not None else "",
            row.tokens,
            row.api.value,
            row.api.provenance.value,
            "yes" if row.low_confidence else "no",
        ]
        for row in report.rows
    ]
    return header, rows


# Every savings waterfall step, in waterfall order (LLD §21.4): baseline, reductions, overheads, net cost.
SAVINGS_STEPS: tuple[str, ...] = get_args(SavingsStep.model_fields["step"].annotation)


def _optimization_table(report: OptimizationReport) -> tuple[list[str], list[list[Any]]]:
    """One row per group. The waterfall columns sum to `waterfall_net_micros` (baseline minus reductions
    plus overheads), each with its own provenance; net savings are baseline minus net."""
    header = [
        "group",
        "label",
        "requests",
        "cim_invoked",
        "cim_bypassed_by_policy",
        "cim_skipped",
        "local_fast",
        "sovereign_direct",
        "cache_hits",
        "context_tokens_before",
        "context_tokens_after",
        "cached_input_tokens",
        "avoided_micros",
        "cim_api_micros",
        "cim_gpu_estimated_micros",
        "retries",
        "retry_api_micros",
        "estimated_net_savings_micros",
        "savings_provenance",
        *(f"waterfall_{step}_{kind}" for step in SAVINGS_STEPS for kind in ("micros", "provenance")),
        "measurement_cost_micros",
        "measurement_cost_provenance",
    ]
    rows = [
        [
            row.group,
            row.label,
            row.request_count,
            row.cim_invoked,
            row.cim_bypassed_by_policy,
            row.cim_skipped,
            row.local_fast,
            row.sovereign_direct,
            row.cache_hits,
            row.context_tokens_before,
            row.context_tokens_after,
            row.cached_input_tokens,
            row.avoided.micros,
            row.cim_overhead.api.value,
            row.cim_overhead.gpu_estimated.value,
            row.retries.retries,
            row.retries.api.value,
            row.savings.net_savings_micros,
            row.savings.provenance.value,
            *_waterfall_cells(row.savings.steps),
            row.savings.measurement_cost.value,
            row.savings.measurement_cost.provenance.value,
        ]
        for row in report.rows
    ]
    return header, rows


def _waterfall_cells(steps: Sequence[SavingsStep]) -> list[Any]:
    by_step: dict[str, SavingsStep] = {step.step: step for step in steps}
    cells: list[Any] = []
    for name in SAVINGS_STEPS:
        step = by_step.get(name)
        cells += [step.amount_micros, step.provenance.value] if step else [0, ""]
    return cells


def _governance_table(report: GovernanceReport) -> tuple[list[str], list[list[Any]]]:
    header = [
        "group",
        "label",
        "requests",
        "allowed",
        "policy_denied",
        "approval_required",
        "quota_denied",
        "failed",
        "confidential_requests",
        "sovereign_direct",
        "cim_bypassed_by_policy",
        "confidential_external_attempts",
        "confidential_cim_attempts",
        "confidential_comparison_attempts",
        "approval_exceptions",
        "denial_reasons",
    ]
    rows = [
        [
            row.group,
            row.label,
            row.request_count,
            row.allowed,
            row.policy_denied,
            row.approval_required,
            row.quota_denied,
            row.failed,
            row.confidential_requests,
            row.sovereign_direct,
            row.cim_bypassed_by_policy,
            row.confidential_external_attempts,
            row.confidential_cim_attempts,
            row.confidential_comparison_attempts,
            row.approval_exceptions,
            "; ".join(f"{r.rule_id}:{r.count}" for r in row.denial_reasons),
        ]
        for row in [*report.rows, report.totals]
    ]
    return header, rows


# --- Reads -----------------------------------------------------------------------------------------


def job_of(row: ReportExportRow, now: datetime) -> ExportJob:
    status = row.status
    error = row.error
    if status in _IN_FLIGHT and now - row.created_at > STALE_AFTER:
        # Shown as failed even before the worker records it (fail_interrupted), e.g. with the worker off.
        status, error = "failed", INTERRUPTED
    if status == "completed" and now >= row.expires_at:
        status = "expired"
    return ExportJob(
        export_id=row.export_id,
        status=status,
        report=row.report,
        group_by=row.group_by,
        window=ReportWindow.model_validate({"from": row.window_from, "to": row.window_to}),
        filters=dict(row.filters),
        created_at=row.created_at,
        completed_at=row.completed_at,
        expires_at=row.expires_at,
        row_count=row.row_count,
        download_url=f"/v1/reports/exports/{row.export_id}/download" if status == "completed" else None,
        error=error,
    )


async def own_export(session: AsyncSession, caller: Caller, export_id: str) -> ReportExportRow:
    """The caller's own export; anyone else's (or another tenant's) is reported as not found."""
    row = await session.get(ReportExportRow, export_id)
    if row is None or row.tenant_id != caller.tenant_id or row.requester_pseudonym != pseudonym_of(caller):
        raise NotFoundError(
            "Export not found.", remediation="Exports can only be downloaded by their requester."
        )
    return row


async def download(
    session: AsyncSession, caller: Caller, export_id: str, *, scope: ReportScope
) -> tuple[ReportExportRow, str]:
    """Return the CSV once per call, audited. Expired exports are purged and refused (410).

    The caller is checked again at download, not only at request: `scope` is the caller's current
    reporting scope, and it must still cover the scope recorded with the export, so a role or department
    removed within the export's lifetime stops the download (403, audited).
    """
    row = await own_export(session, caller, export_id)
    if not scope.covers(ReportScope.from_snapshot(row.scope)):
        session.add(_audit(caller, "report.export.download_refused", row, {"reason": "scope_changed"}))
        raise PermissionDeniedError(
            "Your reporting scope no longer covers this export.",
            remediation="Request a new export within your current scope.",
        )
    now = datetime.now(UTC)
    if now >= row.expires_at:
        if row.content is not None:
            row.content = None
        raise ExportExpiredError(
            "This export has expired and its file was deleted.", remediation="Request a new export."
        )
    if row.status != "completed" or row.content is None:
        raise ExportNotReadyError("This export is not ready yet.", remediation="Check its status and retry.")
    session.add(_audit(caller, "report.export.download", row, {"sha256": row.content_sha256}))
    return row, row.content


async def fail_interrupted(
    session: AsyncSession, tenant_ids: Sequence[str], *, created_before: datetime
) -> list[str]:
    """Record queued or running exports created before `created_before` as failed (`interrupted`), with an
    audit row each. Exports render in a task of the gateway process, so a restart orphans the ones in flight:
    the worker calls this at start for every export created before the process started, then each interval
    for any still in flight after `STALE_AFTER`. The caller commits."""
    now = datetime.now(UTC)
    in_flight: ColumnElement[bool] = ReportExportRow.status.in_(_IN_FLIGHT)
    rows = (
        await session.execute(
            update(ReportExportRow)
            .where(
                ReportExportRow.tenant_id.in_(list(tenant_ids)),
                in_flight,
                ReportExportRow.created_at < created_before,
            )
            .values(status="failed", error=INTERRUPTED, completed_at=now)
            .returning(
                ReportExportRow.export_id,
                ReportExportRow.tenant_id,
                ReportExportRow.report,
                ReportExportRow.group_by,
                ReportExportRow.window_from,
                ReportExportRow.window_to,
            )
        )
    ).all()
    for row in rows:
        session.add(
            AuditLogRow(
                tenant_id=row.tenant_id,
                actor_kind="system",
                actor_id="aiva-gateway",
                action="report.export.interrupted",
                target_type="report_export",
                target_id=row.export_id,
                details={
                    "report": row.report,
                    "group_by": row.group_by,
                    "window": [row.window_from.isoformat(), row.window_to.isoformat()],
                },
            )
        )
    return [row.export_id for row in rows]


async def purge_expired(session: AsyncSession, tenant_ids: Sequence[str]) -> int:
    """Delete the content of expired exports (LLD §21.8 export expiry). Job metadata is kept."""
    result = await session.execute(
        update(ReportExportRow)
        .where(
            ReportExportRow.tenant_id.in_(list(tenant_ids)),
            ReportExportRow.expires_at <= datetime.now(UTC),
            ReportExportRow.content.is_not(None),
        )
        .values(content=None)
    )
    return int(getattr(result, "rowcount", 0) or 0)
