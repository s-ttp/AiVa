"""The overview's traffic flow (Story mode): every request in exactly one bucket, stopped before any route
or the route it took, reconciled with the run records."""

from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from aiva_gateway.db.models import ExecutionRunRow, OptimizationEventRow
from aiva_gateway.reporting.totals import flow_bucket
from tests.insights.conftest import refresh_window

TODAY = datetime.now(UTC).date()
WINDOW = {"from": TODAY.isoformat(), "to": TODAY.isoformat()}
DAY_START = datetime.combine(TODAY, datetime.min.time(), tzinfo=UTC)


@pytest.mark.parametrize(
    ("outcome", "path", "strategy", "bucket"),
    [
        ("policy_denied", "none", "none", "policy_denied"),
        ("approval_required", "none", "none", "approval_required"),
        ("quota_denied", "none", "none", "quota_denied"),
        ("error", "none", "none", "not_routed"),
        ("success", "cache-return", "none", "cache"),
        ("success", "sovereign-direct", "full", "sovereign"),
        ("success", "local-fast", "full", "local_fast"),
        ("success", "cim-assisted", "evidence-preserving", "cim_packaged"),
        ("success", "cim-assisted", "prune", "cim_packaged"),
        ("success", "cim-assisted", "full", "full_context"),
        ("success", "cim-assisted", "none", "full_context"),
        # A run that failed after routing still went its way.
        ("error", "sovereign-direct", "full", "sovereign"),
        ("timeout", "cim-assisted", "evidence-preserving", "cim_packaged"),
    ],
)
def test_each_run_has_one_flow_bucket(outcome: str, path: str, strategy: str, bucket: str) -> None:
    assert flow_bucket(outcome, path, strategy) == bucket


async def test_the_traffic_flow_partitions_the_requests(traffic, services, make_client, user_token, db):
    await refresh_window(services, DAY_START, DAY_START + timedelta(days=1))
    async with make_client(user_token("u-finance")) as client:
        response = await client.get("/v1/reports/overview", params=WINDOW)
        by_strategy = await client.get("/v1/reports/optimization", params={**WINDOW, "group_by": "strategy"})
    assert response.status_code == by_strategy.status_code == 200, response.text
    body = response.json()
    flow = body["flow"]

    run, event, latest_event = ExecutionRunRow, OptimizationEventRow, aliased(OptimizationEventRow)
    latest = select(func.max(latest_event.id)).where(latest_event.run_id == run.run_id).correlate(run)
    strategy = (
        select(event.context_strategy).where(event.id == latest.scalar_subquery()).correlate(run)
    ).scalar_subquery()
    rows = await db.execute(
        select(run.outcome, run.execution_path, func.coalesce(strategy, "none")).where(
            run.tenant_id == "gct",
            run.created_at >= DAY_START,
            run.created_at < DAY_START + timedelta(days=1),
        )
    )
    expected = Counter(flow_bucket(*row) for row in rows)

    assert [b["bucket"] for b in flow["stopped"]] == [
        "policy_denied",
        "approval_required",
        "quota_denied",
        "not_routed",
    ]
    assert [b["bucket"] for b in flow["routed"]] == [
        "cache",
        "sovereign",
        "local_fast",
        "cim_packaged",
        "full_context",
    ]
    counts = {b["bucket"]: b["count"] for b in flow["stopped"] + flow["routed"]}
    assert counts == {bucket: expected.get(bucket, 0) for bucket in counts}
    assert sum(counts.values()) == flow["requests"] == body["usage"]["request_count"] > 0
    # The core traffic plan exercises every route and the policy refusal.
    for bucket in ("policy_denied", "cache", "sovereign", "local_fast", "cim_packaged", "full_context"):
        assert counts[bucket] > 0, bucket
    assert counts["policy_denied"] == body["outcomes"]["policy_denied"]
    # The same counts as read off the optimization report by context strategy.
    rows = by_strategy.json()["rows"]
    assert counts["cache"] == sum(r["cache_hits"] for r in rows)
    assert counts["sovereign"] == sum(r["sovereign_direct"] for r in rows)
    assert counts["local_fast"] == sum(r["local_fast"] for r in rows)
    assert counts["cim_packaged"] == sum(
        r["request_count"] for r in rows if r["group"] not in ("full", "none")
    )
    assert flow["provenance"] == "synthetic"
