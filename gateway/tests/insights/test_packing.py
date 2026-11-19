"""Packing measured as passages kept (Story mode): on the requests that sent the CIM's package, how many of
the passages it was given it kept, and the input tokens the answering model read. Counted with the run
view's rule, so the weekly figures and a run agree."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, inspect, select

from aiva_gateway.db.models import (
    ContextPackageRow,
    ExecutionRunRow,
    ModelAttemptRow,
    OptimizationEventRow,
)
from aiva_gateway.optimize.cim import passage_counts
from aiva_gateway.reporting.aggregate import PACKAGE_SENT
from tests.insights.conftest import refresh_window

TODAY = datetime.now(UTC).date()
DAY_START = datetime.combine(TODAY, datetime.min.time(), tzinfo=UTC)


async def _overview(client: Any, day: str) -> dict[str, Any]:
    response = await client.get("/v1/reports/overview", params={"from": day, "to": day})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_packing_reconciles_with_the_packages_and_attempts(
    traffic, services, make_client, user_token, db
):
    await refresh_window(services, DAY_START, DAY_START + timedelta(days=1))
    async with make_client(user_token("u-finance")) as client:
        body = await _overview(client, TODAY.isoformat())
        by_model = await client.get(
            "/v1/reports/optimization",
            params={"from": TODAY.isoformat(), "to": TODAY.isoformat(), "group_by": "model"},
        )
    packing = body["packing"]

    run = ExecutionRunRow
    today = [
        run.tenant_id == "gct",
        run.created_at >= DAY_START,
        run.created_at < DAY_START + timedelta(days=1),
    ]
    latest = select(func.max(ContextPackageRow.id)).join(run, run.run_id == ContextPackageRow.run_id)
    latest = latest.where(*today).group_by(ContextPackageRow.run_id)
    packages = list(await db.scalars(select(ContextPackageRow).where(ContextPackageRow.id.in_(latest))))
    sent = [p for p in packages if p.validation in PACKAGE_SENT]
    counts = [passage_counts(p.evidence_refs, p.retained_ids) for p in sent]
    first_calls = await db.scalar(
        select(func.sum(ModelAttemptRow.input_tokens)).where(
            ModelAttemptRow.run_id.in_([p.run_id for p in sent]),
            ModelAttemptRow.stage == "upstream",
            ModelAttemptRow.retry_of.is_(None),
        )
    )

    assert sent, "the traffic plan sends CIM packages"
    assert (
        packing["packed_runs"]
        == len(sent)
        == next(b["count"] for b in body["flow"]["routed"] if b["bucket"] == "cim_packaged")
    )
    assert packing["passages_retrieved"] == sum(c.retrieved for c in counts) > 0
    assert packing["passages_kept"] == sum(c.kept for c in counts)
    assert packing["passages_kept"] <= packing["passages_retrieved"]
    assert packing["model_input_tokens"]["total"] == first_calls > 0
    assert packing["provenance"] == "synthetic"
    assert packing["model_input_tokens"]["provenance"] != "measured"
    # The optimization report carries the same figures overall, and its rows partition them.
    assert by_model.status_code == 200
    report = by_model.json()
    assert report["packing"] == packing
    assert sum(r["packing"]["passages_kept"] for r in report["rows"]) == packing["passages_kept"]


async def _clone_run(db: Any, source_id: str, run_id: str, at: datetime) -> ExecutionRunRow:
    source = await db.get(ExecutionRunRow, source_id)
    values = {a.key: getattr(source, a.key) for a in inspect(ExecutionRunRow).mapper.column_attrs}
    values.update(
        run_id=run_id,
        trace_id=run_id[-32:].rjust(32, "0"),
        created_at=at,
        completed_at=at,
        execution_path="cim-assisted",
        outcome="success",
        cim_state="invoked",
    )
    clone = ExecutionRunRow(**values)
    db.add(clone)
    await db.commit()
    return clone


async def test_only_a_sent_package_counts_with_the_run_views_rule(
    traffic, services, make_client, user_token, db
):
    """A verified package given a duplicate id and keeping an unknown one, and a package the Guard refused
    (full-context fallback), on a day of their own."""
    day = datetime(2026, 2, 7, 9, tzinfo=UTC)
    plans = [
        ("verified", "evidence-preserving", ["ev-1", "ev-2", "ev-2", "ev-3"], ["ev-2", "ev-9"]),
        ("failed-fallback-applied", "full", ["ev-4", "ev-5"], ["ev-4"]),
    ]
    for index, (validation, strategy, given, kept) in enumerate(plans):
        clone = await _clone_run(db, traffic.report.runs[0], f"run_packing_test_{index:02d}", day)
        db.add(
            ContextPackageRow(
                run_id=clone.run_id,
                cim_revision="test",
                context_profile="fraud-case",
                evidence_refs=given,
                retained_ids=kept,
                validation=validation,
                guard_details={"failures": []},
                provenance="simulated",
            )
        )
        db.add(OptimizationEventRow(run_id=clone.run_id, context_strategy=strategy))
    await db.commit()
    await refresh_window(services, day.replace(hour=0), day.replace(hour=0) + timedelta(days=1))
    async with make_client(user_token("u-finance")) as client:
        packing = (await _overview(client, "2026-02-07"))["packing"]
    assert passage_counts(plans[0][2], plans[0][3]).retrieved == 3
    assert (packing["packed_runs"], packing["passages_retrieved"], packing["passages_kept"]) == (1, 3, 1)
