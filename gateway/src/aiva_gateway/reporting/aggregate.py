"""Insights aggregation worker (LLD §21.2, §21.5; ADR-0015).

Builds `report_aggregate_hourly` from the run records (execution runs, classification and optimization
events, model attempts, the usage ledger, reservations and approvals) and rolls each touched day up into
`report_aggregate_daily`. Usage is attributed to the UTC hour its run started. Every refresh rebuilds whole
hours (delete + insert) under a per-tenant advisory lock, so it is idempotent and safe to re-run for any
window; summed lines reproduce the attempt-level ledger exactly.

Incremental refreshes rebuild the hours of runs recorded, and of ledger rows written (late corrections),
since the previous completed refresh started, minus a safety margin for transactions still committing.
"""

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import delete, func, insert, literal, select, union
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from aiva_gateway.catalog import Catalog
from aiva_gateway.core.enums import PolicyAction, PolicyStage
from aiva_gateway.core.ids import new_id
from aiva_gateway.db import models as db
from aiva_gateway.finops.pricing import PriceBook
from aiva_gateway.optimize.cim import passage_counts
from aiva_gateway.reporting.dimensions import DIMENSIONS, run_dimension_columns
from aiva_gateway.reporting.lines import (
    MEASURES,
    QUALITY_STAGES,
    AttemptFact,
    LedgerFact,
    Line,
    Pricing,
    RunFacts,
    SourceAnswer,
    add_into,
    quality_exception_run,
    run_lines,
    served_attempt,
)

logger = logging.getLogger(__name__)

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
# Runs and ledger rows are written by other transactions; rebuild hours touched this long before the
# previous refresh started, so nothing that committed late is missed.
MARGIN = timedelta(minutes=10)
# Context Guard outcomes under which the CIM's package was sent upstream (not the full-context fallback).
PACKAGE_SENT = ("verified", "cim-selected")

# Evaluates anomaly rules for refreshed hours and returns the number of alerts raised.
AnomalyHook = Callable[[str, Sequence[datetime]], Awaitable[int]]


def floor_hour(at: datetime) -> datetime:
    return at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


def hours_between(start: datetime, end: datetime) -> list[datetime]:
    hours, current = [], floor_hour(start)
    while current < end:
        hours.append(current)
        current += HOUR
    return hours


@dataclass
class RefreshResult:
    refresh_id: str
    tenant_id: str
    triggered_by: str
    started_at: datetime
    window_start: datetime | None = None
    window_end: datetime | None = None
    status: str = "running"
    hours: list[datetime] = field(default_factory=list)
    days: list[date] = field(default_factory=list)
    runs_counted: int = 0
    lines_written: int = 0
    alerts_raised: int = 0
    error: str | None = None
    completed_at: datetime | None = None

    def row(self) -> db.ReportRefreshRow:
        return db.ReportRefreshRow(
            refresh_id=self.refresh_id,
            tenant_id=self.tenant_id,
            triggered_by=self.triggered_by,
            status=self.status,
            window_start=self.window_start,
            window_end=self.window_end,
            hours_refreshed=len(self.hours),
            days_refreshed=len(self.days),
            runs_counted=self.runs_counted,
            lines_written=self.lines_written,
            alerts_raised=self.alerts_raised,
            error=self.error,
            started_at=self.started_at,
            completed_at=self.completed_at,
        )


def build_pricing(catalog: Catalog, prices: PriceBook) -> Pricing:
    return Pricing(
        book=prices,
        frontier_model_id=catalog.models.frontier_baseline,
        external_models=frozenset(m.id for m in catalog.models.models if m.external),
    )


class Aggregator:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        catalog: Catalog,
        prices: PriceBook,
        *,
        anomalies: AnomalyHook | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.sessionmaker = sessionmaker
        self.catalog = catalog
        self.pricing = build_pricing(catalog, prices)
        self.threshold = catalog.taxonomy.review_confidence_threshold
        self.anomalies = anomalies
        self.clock = clock

    async def refresh(
        self,
        tenant_id: str,
        *,
        triggered_by: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> RefreshResult:
        """Rebuild the hours in [start, end), or every hour touched since the last refresh."""
        result = RefreshResult(
            refresh_id=new_id("rfr"),
            tenant_id=tenant_id,
            triggered_by=triggered_by,
            started_at=self.clock(),
            window_start=start,
            window_end=end,
        )
        try:
            if start is not None and end is not None:
                result.hours = hours_between(start, end)
            else:
                result.hours = await self._dirty_hours(tenant_id)
            by_day: dict[date, list[datetime]] = defaultdict(list)
            for hour in result.hours:
                by_day[hour.date()].append(hour)
            for day in sorted(by_day):
                await self._rebuild_day(tenant_id, day, by_day[day], result)
                result.days.append(day)
            if self.anomalies is not None and result.hours:
                result.alerts_raised = await self.anomalies(tenant_id, result.hours)
            result.status = "completed"
        except (SQLAlchemyError, OSError, ValueError) as exc:
            result.status, result.error = "failed", f"{type(exc).__name__}: {exc}"[:500]
            logger.warning("Insights refresh %s failed", result.refresh_id, exc_info=True)
        result.completed_at = self.clock()
        try:
            async with self.sessionmaker() as session, session.begin():
                session.add(result.row())
        except SQLAlchemyError:
            logger.warning("Could not record Insights refresh %s", result.refresh_id, exc_info=True)
        return result

    async def last_completed(self, session: AsyncSession, tenant_id: str) -> db.ReportRefreshRow | None:
        refresh = db.ReportRefreshRow
        return await session.scalar(
            select(refresh)
            .where(refresh.tenant_id == tenant_id, refresh.status == "completed")
            .order_by(refresh.started_at.desc())
            .limit(1)
        )

    async def _dirty_hours(self, tenant_id: str) -> list[datetime]:
        run, ledger = db.ExecutionRunRow, db.UsageLedgerRow
        hour = func.date_trunc("hour", run.created_at, "UTC")
        async with self.sessionmaker() as session:
            previous = await self.last_completed(session, tenant_id)
            if previous is None:
                statement = select(hour.label("hour")).where(run.tenant_id == tenant_id).distinct()
            else:
                since = previous.started_at - MARGIN
                recorded = select(hour.label("hour")).where(
                    run.tenant_id == tenant_id, func.coalesce(run.completed_at, run.created_at) >= since
                )
                corrected = (
                    select(hour.label("hour"))
                    .join(ledger, ledger.run_id == run.run_id)
                    .where(ledger.tenant_id == tenant_id, ledger.created_at >= since)
                )
                statement = select(union(recorded, corrected).subquery().c.hour)
            rows = (await session.execute(statement)).scalars().all()
        return sorted({floor_hour(value) for value in rows})

    async def _rebuild_day(
        self, tenant_id: str, day: date, hours: Iterable[datetime], result: RefreshResult
    ) -> None:
        hourly, daily = db.ReportAggregateHourlyRow, db.ReportAggregateDailyRow
        now = self.clock()
        async with self.sessionmaker() as session, session.begin():
            lock_key = func.hashtext(f"aiva-insights:{tenant_id}")
            await session.execute(select(func.pg_advisory_xact_lock(lock_key)))
            for hour in hours:
                totals, runs = await self._hour_lines(session, tenant_id, hour)
                await session.execute(
                    delete(hourly).where(hourly.tenant_id == tenant_id, hourly.bucket_start == hour)
                )
                if totals:
                    stamp = {"tenant_id": tenant_id, "bucket_start": hour, "refreshed_at": now}
                    rows = [{**line, **stamp, "line_key": key} for key, line in totals.items()]
                    await session.execute(insert(hourly), rows)
                result.runs_counted += runs
                result.lines_written += len(totals)
            start = day_start(day)
            await session.execute(delete(daily).where(daily.tenant_id == tenant_id, daily.day == day))
            dims = [getattr(hourly, name) for name in DIMENSIONS]
            sums = [func.sum(getattr(hourly, name)) for name in MEASURES]
            source = (
                select(literal(day), hourly.tenant_id, hourly.line_key, *dims, *sums, literal(now))
                .where(
                    hourly.tenant_id == tenant_id,
                    hourly.bucket_start >= start,
                    hourly.bucket_start < start + DAY,
                )
                .group_by(hourly.tenant_id, hourly.line_key, *dims)
            )
            columns = ["day", "tenant_id", "line_key", *DIMENSIONS, *MEASURES, "refreshed_at"]
            await session.execute(insert(daily).from_select(columns, source))

    async def _hour_lines(
        self, session: AsyncSession, tenant_id: str, hour: datetime
    ) -> tuple[dict[str, Line], int]:
        run = db.ExecutionRunRow
        facts = await load_run_facts(
            session,
            self.threshold,
            run.tenant_id == tenant_id,
            run.created_at >= hour,
            run.created_at < hour + HOUR,
        )
        totals: dict[str, Line] = {}
        for item in facts.values():
            add_into(totals, run_lines(item, self.pricing))
        return totals, len(facts)


async def load_run_facts(
    session: AsyncSession, threshold: float, *where: ColumnElement[bool]
) -> dict[str, RunFacts]:
    """Everything the line builder needs for the runs matching `where` (columns of execution_run)."""
    run = db.ExecutionRunRow
    statement, dims = run_dimension_columns(threshold)
    rows = (
        await session.execute(
            statement.add_columns(*(column.label(name) for name, column in dims.items())).where(*where)
        )
    ).all()
    facts: dict[str, RunFacts] = {}
    for row in rows:
        record: db.ExecutionRunRow = row[0]
        facts[record.run_id] = RunFacts(
            run_id=record.run_id,
            dimensions={name: str(getattr(row, name)) for name in dims},
            execution_path=record.execution_path,
            provenance=record.provenance,
            latency_ms=record.latency_ms,
            quality_exception=quality_exception_run(
                tenant_id=record.tenant_id,
                scenario_id=record.scenario_id,
                classification=record.effective_classification,
                routing_mode=record.routing_mode,
                data_designation=record.data_designation,
            ),
        )
    if not facts:
        return facts
    run_ids = select(run.run_id).where(*where)
    await _load_attempts(session, run_ids, facts)
    await _load_ledger(session, run_ids, facts)
    await _load_events(session, run_ids, facts)
    await _load_comparison_policy(session, run_ids, facts)
    return facts


async def _load_attempts(session: AsyncSession, run_ids: Any, facts: dict[str, RunFacts]) -> None:
    attempt = db.ModelAttemptRow
    rows = await session.scalars(
        select(attempt).where(attempt.run_id.in_(run_ids)).order_by(attempt.run_id, attempt.sequence)
    )
    for row in rows:
        facts[row.run_id].attempts.append(attempt_fact(row))


async def _load_ledger(session: AsyncSession, run_ids: Any, facts: dict[str, RunFacts]) -> None:
    ledger = db.UsageLedgerRow
    rows = await session.scalars(
        select(ledger).where(ledger.run_id.in_(run_ids)).order_by(ledger.created_at, ledger.ledger_id)
    )
    for row in rows:
        facts[row.run_id].ledger.append(
            LedgerFact(
                attempt_id=row.attempt_id,
                charge_type=row.charge_type,
                amount_micros=row.amount_micros,
                provenance=row.measured_or_estimated,
                rate_card_version=row.rate_card_version,
            )
        )


async def _load_comparison_policy(session: AsyncSession, run_ids: Any, facts: dict[str, RunFacts]) -> None:
    """The answer-quality comparison calls (ADR-0023) that their own egress policy events allowed. A call's
    events are those of its run for its model under the comparison's reservation, which its ledger row
    carries: a comparison is reserved apart from the run it compares. Every such event must allow it."""
    attempt, ledger, event = db.ModelAttemptRow, db.UsageLedgerRow, db.PolicyEventRow
    rows = await session.execute(
        select(attempt.run_id, attempt.attempt_id, event.action)
        .join(ledger, ledger.attempt_id == attempt.attempt_id)
        .join(
            event,
            (event.run_id == attempt.run_id)
            & (event.stage == PolicyStage.EGRESS.value)
            & (event.egress_model_id == attempt.model_id)
            & (event.reservation_id == ledger.reservation_id),
        )
        .where(attempt.run_id.in_(run_ids), attempt.stage.in_(QUALITY_STAGES))
    )
    actions: dict[tuple[str, str], set[str]] = defaultdict(set)
    for run_id, attempt_id, action in rows:
        actions[(run_id, attempt_id)].add(action)
    for (run_id, attempt_id), seen in actions.items():
        if seen == {PolicyAction.ALLOW.value}:
            facts[run_id].allowed_comparisons.add(attempt_id)


async def _load_events(session: AsyncSession, run_ids: Any, facts: dict[str, RunFacts]) -> None:
    optimization = db.OptimizationEventRow
    for event in await session.scalars(
        select(optimization).where(optimization.run_id.in_(run_ids)).order_by(optimization.id)
    ):
        item = facts[event.run_id]  # the latest event per run wins
        item.cache_hit = event.cache_hit
        item.avoided_call = event.avoided_call
        item.avoided_tokens = event.modeled_avoided_tokens
        item.avoided_micros = event.modeled_avoided_micros
        item.context_tokens_before = event.context_tokens_before
        item.context_tokens_after = event.context_tokens_after
        item.removed_passages = event.removed_passages_count
    package = db.ContextPackageRow
    for stored in await session.scalars(
        select(package).where(package.run_id.in_(run_ids)).order_by(package.id)
    ):
        item = facts[stored.run_id]  # the latest package per run wins
        sent = stored.validation in PACKAGE_SENT
        counts = passage_counts(stored.evidence_refs, stored.retained_ids)
        item.passages_retrieved = counts.retrieved if sent else 0
        item.passages_kept = counts.kept if sent else 0
    assurance = db.AssuranceResultRow
    for check in await session.scalars(
        select(assurance).where(assurance.run_id.in_(run_ids)).order_by(assurance.id)
    ):
        facts[check.run_id].quality_passed = check.passed
    reservation = db.BudgetReservationRow
    for held in await session.scalars(select(reservation).where(reservation.run_id.in_(run_ids))):
        if held.run_id not in facts:
            continue
        facts[held.run_id].reserved_micros += held.estimate_micros
        if held.status == "settled":
            facts[held.run_id].committed_micros += held.actual_micros
    approval = db.ApprovalRequestRow
    consumed = await session.execute(
        select(approval.consumed_by_run_id, func.count())
        .where(approval.consumed_by_run_id.in_(run_ids))
        .group_by(approval.consumed_by_run_id)
    )
    for run_id, count in consumed:
        if run_id is not None:
            facts[run_id].approvals_consumed = int(count)
    cache = db.CacheEventRow
    hits = await session.execute(
        select(cache.run_id, cache.source_run_id).where(
            cache.run_id.in_(run_ids), cache.hit.is_(True), cache.source_run_id.is_not(None)
        )
    )
    sources = {run_id: str(source_id) for run_id, source_id in hits}
    if sources:
        attempt = db.ModelAttemptRow
        answers: dict[str, list[AttemptFact]] = defaultdict(list)
        for row in await session.scalars(
            select(attempt)
            .where(attempt.run_id.in_(set(sources.values())))
            .order_by(attempt.run_id, attempt.sequence)
        ):
            answers[row.run_id].append(attempt_fact(row))
        for run_id, source_id in sources.items():
            # The tokens the cache entry models: every upstream call of the source run, as FinOps' direct
            # estimate prices them (CachedResponse.modeled_*_tokens), so the two agree on every hit.
            served = served_attempt(answers.get(source_id, []))
            upstream = [a for a in answers.get(source_id, []) if a.stage == "upstream"]
            if served is not None:
                facts[run_id].cache_source = SourceAnswer(
                    served.model_id,
                    sum(a.input_tokens for a in upstream),
                    sum(a.output_tokens for a in upstream),
                )


def attempt_fact(row: db.ModelAttemptRow) -> AttemptFact:
    return AttemptFact(
        attempt_id=row.attempt_id,
        stage=row.stage,
        model_id=row.model_id,
        provider=row.provider,
        status=row.status,
        retry_of=row.retry_of,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cached_input_tokens=row.cached_input_tokens,
        reasoning_tokens=row.reasoning_tokens,
        latency_ms=row.latency_ms,
        provenance=row.provenance,
    )
