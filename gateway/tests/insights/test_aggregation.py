"""Aggregation worker: exact reconciliation to the attempt-level ledger, idempotence, daily roll-up."""

from datetime import UTC, datetime

from sqlalchemy import func, select

from aiva_gateway.db.models import (
    ExecutionRunRow,
    ModelAttemptRow,
    ReportAggregateDailyRow,
    ReportAggregateHourlyRow,
    UsageLedgerRow,
)
from aiva_gateway.reporting.lines import MEASURES
from tests.insights.conftest import GeneratedTraffic, refresh_window


def _window(model, column, traffic: GeneratedTraffic):
    return (model.tenant_id == "gct", column >= traffic.start, column < traffic.end)


async def _ledger_totals(db, traffic: GeneratedTraffic) -> dict[str, int]:
    run = ExecutionRunRow
    in_window = select(run.run_id).where(*_window(run, run.created_at, traffic))
    attempt, ledger = ModelAttemptRow, UsageLedgerRow
    tokens = (
        await db.execute(
            select(
                func.coalesce(func.sum(attempt.input_tokens), 0),
                func.coalesce(func.sum(attempt.output_tokens), 0),
                func.coalesce(func.sum(attempt.cached_input_tokens), 0),
                func.count(),
            ).where(attempt.run_id.in_(in_window))
        )
    ).one()
    api = await db.scalar(
        select(func.coalesce(func.sum(ledger.amount_micros), 0)).where(
            ledger.run_id.in_(in_window), ledger.charge_type != "gpu_estimate"
        )
    )
    gpu = await db.scalar(
        select(func.coalesce(func.sum(ledger.amount_micros), 0)).where(
            ledger.run_id.in_(in_window), ledger.charge_type == "gpu_estimate"
        )
    )
    runs = await db.scalar(
        select(func.count()).select_from(run).where(*_window(run, run.created_at, traffic))
    )
    return {
        "input_tokens": int(tokens[0]),
        "output_tokens": int(tokens[1]),
        "cached_input_tokens": int(tokens[2]),
        "attempt_count": int(tokens[3]),
        "api_micros": int(api),
        "gpu_estimated_micros": int(gpu),
        "request_count": int(runs),
    }


async def _aggregate_totals(db, model, column, traffic: GeneratedTraffic) -> dict[str, int]:
    names = [
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "attempt_count",
        "api_micros",
        "gpu_estimated_micros",
        "request_count",
    ]
    row = (
        await db.execute(
            select(*(func.coalesce(func.sum(getattr(model, n)), 0) for n in names)).where(
                *_window(model, column, traffic)
            )
        )
    ).one()
    return {name: int(value) for name, value in zip(names, row, strict=True)}


async def test_hourly_totals_reconcile_exactly_to_the_attempt_ledger(traffic, db) -> None:
    expected = await _ledger_totals(db, traffic)
    assert expected["request_count"] >= len(traffic.report.runs)
    hourly = ReportAggregateHourlyRow
    assert await _aggregate_totals(db, hourly, hourly.bucket_start, traffic) == expected


async def test_daily_rollup_equals_the_hours(traffic, db) -> None:
    hourly, daily = ReportAggregateHourlyRow, ReportAggregateDailyRow
    day = traffic.start.astimezone(UTC).date()
    sums = [func.coalesce(func.sum(getattr(hourly, n)), 0) for n in MEASURES]
    by_hours = (
        await db.execute(
            select(*sums).where(
                hourly.tenant_id == "gct", func.date(func.timezone("UTC", hourly.bucket_start)) == day
            )
        )
    ).one()
    by_day = (
        await db.execute(
            select(*(func.coalesce(func.sum(getattr(daily, n)), 0) for n in MEASURES)).where(
                daily.tenant_id == "gct", daily.day == day
            )
        )
    ).one()
    assert tuple(by_hours) == tuple(by_day)


async def test_refresh_is_idempotent(traffic, services, db) -> None:
    hourly = ReportAggregateHourlyRow
    columns = [hourly.bucket_start, hourly.line_key, *(getattr(hourly, n) for n in MEASURES)]
    query = (
        select(*columns)
        .where(*_window(hourly, hourly.bucket_start, traffic))
        .order_by(hourly.bucket_start, hourly.line_key)
    )
    before = (await db.execute(query)).all()
    await refresh_window(services, traffic.start, traffic.end)
    await refresh_window(services, traffic.start, traffic.end)
    after = (await db.execute(query)).all()
    assert before == after and before


async def test_incremental_refresh_picks_up_new_runs(traffic, services, db, app_client) -> None:
    from tests.conftest import chat_body
    from tests.insights.conftest import aggregator_for

    aggregator = aggregator_for(services)
    first = await aggregator.refresh("gct", triggered_by="test")
    assert first.status == "completed"
    async with app_client("gct-support-chatbot") as client:
        response = await client.post(
            "/v1/ai/chat", json=chat_body("What are the Europe roaming rates today?")
        )
    assert response.status_code == 200
    second = await aggregator.refresh("gct", triggered_by="test")
    assert second.status == "completed" and second.hours
    now = datetime.now(UTC)
    assert any(hour <= now < hour.replace(minute=59, second=59) for hour in second.hours)
