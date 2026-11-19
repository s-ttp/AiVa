"""GET /v1/reports/quality (LLD §15, §21.7, ADR-0018): reconciles with the run records, maps Guard failures to
codes, reports citation coverage only once measured, never turns 0/0 into a rate, and keeps the reports'
scope, filter and provenance rules."""

from datetime import UTC, datetime, timedelta
from typing import Any, get_args

import pytest
from sqlalchemy import func, inspect, select

from aiva_gateway.contracts.reports import GuardFailureKind
from aiva_gateway.db.models import (
    AssuranceResultRow,
    ContextPackageRow,
    ExecutionRunRow,
    OptimizationEventRow,
)
from aiva_gateway.reporting.quality import _GUARD_CODES, guard_failure_kind
from tests.integration.test_public_demo import clear_limits, start_session  # noqa: F401

TODAY = datetime.now(UTC).date()
WINDOW = {"from": TODAY.isoformat(), "to": TODAY.isoformat()}
DAY_START = datetime.combine(TODAY, datetime.min.time(), tzinfo=UTC)


@pytest.fixture
def as_user(make_client, user_token):
    return lambda user: make_client(user_token(user))


async def _quality(client: Any, **params: str) -> dict[str, Any]:
    response = await client.get("/v1/reports/quality", params={**WINDOW, **params})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _clone_run(db: Any, source_id: str, run_id: str, at: datetime, **changes: Any) -> ExecutionRunRow:
    source = await db.get(ExecutionRunRow, source_id)
    values = {a.key: getattr(source, a.key) for a in inspect(ExecutionRunRow).mapper.column_attrs}
    values.update(
        run_id=run_id, trace_id=run_id[-32:].rjust(32, "0"), created_at=at, completed_at=at, **changes
    )
    clone = ExecutionRunRow(**values)
    db.add(clone)
    await db.commit()
    return clone


async def test_the_quality_report_reconciles_with_the_run_records(traffic, as_user, db) -> None:
    async with as_user("u-finance") as client:
        body = await _quality(client)
    run = ExecutionRunRow
    in_window = [
        run.tenant_id == "gct",
        run.created_at >= DAY_START,
        run.created_at < DAY_START + timedelta(days=1),
    ]
    runs = await db.scalar(select(func.count()).select_from(run).where(*in_window))
    latest = select(func.max(AssuranceResultRow.id)).join(run, run.run_id == AssuranceResultRow.run_id)
    assurance_ids = latest.where(*in_window).group_by(AssuranceResultRow.run_id)
    results = list(
        await db.scalars(select(AssuranceResultRow).where(AssuranceResultRow.id.in_(assurance_ids)))
    )
    cim = dict(
        (
            await db.execute(select(run.cim_state, func.count()).where(*in_window).group_by(run.cim_state))
        ).all()
    )
    latest_event = select(func.max(OptimizationEventRow.id)).join(
        run, run.run_id == OptimizationEventRow.run_id
    )
    event_ids = latest_event.where(*in_window, run.cim_state == "invoked").group_by(
        OptimizationEventRow.run_id
    )
    sufficiency = dict(
        (
            await db.execute(
                select(OptimizationEventRow.preservation_result, func.count())
                .where(OptimizationEventRow.id.in_(event_ids))
                .group_by(OptimizationEventRow.preservation_result)
            )
        ).all()
    )

    totals = body["totals"]
    assert totals["request_count"] == runs > 0
    assurance = totals["assurance"]
    assert assurance["checked_runs"] == len(results)
    assert assurance["passed"] == {
        "numerator": sum(r.passed for r in results),
        "denominator": len(results),
        "value": sum(r.passed for r in results) / len(results),
    }
    dlp = {"clean": 0, "redacted": 0, "blocked": 0, "not_run": 0}
    for r in results:
        dlp[r.output_dlp if r.output_dlp in dlp else "not_run"] += 1
    assert assurance["output_dlp"] == dlp
    sufficiency_block = totals["sufficiency"]
    assert sufficiency_block["cim_invoked_runs"] == cim.get("invoked", 0)
    assert sufficiency_block["verified"]["numerator"] == sufficiency.get("verified", 0)
    assert sufficiency_block["bypassed_by_policy"] == {
        "numerator": cim.get("bypassed-policy", 0),
        "denominator": runs,
        "value": cim.get("bypassed-policy", 0) / runs,
    }
    # Groups and the trend partition the totals.
    assert sum(row["request_count"] for row in body["rows"]) == runs
    assert sum(point["request_count"] for point in body["trend"]) == runs
    assert sum(point["checked_runs"] for point in body["trend"]) == len(results)
    assert body["metadata"]["aggregates_as_of"] is None and body["computed_at"]
    # Synthetic traffic over simulated inference is never labelled measured.
    assert assurance["provenance"] in ("simulated", "replayed", "synthetic")
    assert sufficiency_block["provenance"] != "measured" and totals["guard"]["provenance"] != "measured"


@pytest.mark.parametrize("group_by", ["application", "context_profile", "execution_path"])
async def test_every_grouping_partitions_the_totals(traffic, as_user, group_by: str) -> None:
    async with as_user("u-finance") as client:
        body = await _quality(client, group_by=group_by)
    assert body["group_by"] == group_by and body["rows"]
    assert sum(row["request_count"] for row in body["rows"]) == body["totals"]["request_count"]
    assert all(row["label"] for row in body["rows"])


def test_guard_failures_map_to_codes() -> None:
    messages = {
        "unknown evidence references: ['ev-9']": "unknown_reference",
        "evidence both selected and omitted: ['ev-1']": "selected_and_omitted",
        "empty selection: all 3 evidence passages omitted": "empty_selection",
        "required field missing: account_id": "required_field_missing",
        "account_id: reported missing but present in the evidence": "reported_missing_but_present",
        "amount: source ev-2 not in selected evidence": "source_not_selected",
        "amount: value not found verbatim in source ev-2": "value_not_verbatim",
        "amount: value not preserved in the upstream payload": "value_not_preserved",
        "dangling citation: 2 citation(s) in the payload resolve to no kept source": "dangling_citation",
        "citation lost: selected evidence ev-3 not in the upstream payload": "citation_lost",
        "citation header lost: selected evidence ev-3 lacks its header": "citation_header_lost",
        "evidence wrapper integrity: 2 open / 1 close tags for 2 passages": "wrapper_integrity",
        "context loss: omitted ev-4; the profile keeps every passage": "context_loss",
        "occurred_at: unparseable timestamp": "chronology",
        "occurred_at: 2 evidence timestamp(s) collapsed or dropped": "chronology",
        "numeric fidelity: 1 number(s) not present in the request or evidence": "numeric_fidelity",
        "token budget exceeded: 900 > 800": "token_budget_exceeded",
        "something new the guard says": "other",
    }
    assert {message: guard_failure_kind(message) for message in messages} == messages


def test_every_guard_failure_kind_is_in_the_contract() -> None:
    """A kind the report maps but the contract lacks would fail response validation; one the contract
    lists but nothing maps is never reported."""
    assert set(get_args(GuardFailureKind)) == _GUARD_CODES | {"other"}


async def test_guard_verdicts_are_reported_as_codes_and_coverage_once_measured(traffic, as_user, db) -> None:
    """A day of hand-made runs: two Guard-checked packages (one failing twice), and assurance results with
    and without citation coverage. Only codes and counts come back."""
    source = traffic.report.runs[0]
    day = datetime(2026, 2, 3, 9, tzinfo=UTC)
    window = {"from": "2026-02-03", "to": "2026-02-03"}
    secret = "account 4471-SECRET-NOTE"  # stands in for anything a failure message might quote
    plans = [
        ({"failures": [], "checks": ["citations"]}, 1.0),
        (
            {
                "failures": [
                    f"amount: value not found verbatim in source {secret}",
                    "token budget exceeded: 9 > 8",
                ]
            },
            0.5,
        ),
        (None, None),
    ]
    for index, (details, coverage) in enumerate(plans):
        clone = await _clone_run(db, source, f"run_quality_test_{index:02d}", day, cim_state="invoked")
        if details is not None:
            db.add(
                ContextPackageRow(
                    run_id=clone.run_id,
                    cim_revision="test",
                    context_profile="fraud-case",
                    validation="verified",
                    guard_details=details,
                    provenance="simulated",
                )
            )
        db.add(
            AssuranceResultRow(
                run_id=clone.run_id,
                passed=True,
                output_dlp="clean",
                checks=["output-dlp"],
                evidence_coverage=coverage,
            )
        )
    await db.commit()
    async with as_user("u-finance") as client:
        body = await _quality(client, **window, group_by="context_profile")
        empty = await _quality(client, **{"from": "2026-02-04", "to": "2026-02-04"})
    guard = body["totals"]["guard"]
    assert guard["packages"] == 2
    assert guard["passed"] == {"numerator": 1, "denominator": 2, "value": 0.5}
    assert {f["kind"]: f["count"] for f in guard["failures"]} == {
        "value_not_verbatim": 1,
        "token_budget_exceeded": 1,
    }
    assert secret not in str(body) and "verbatim in source" not in str(body)
    coverage = body["totals"]["evidence_coverage"]
    assert coverage["status"] == "measured" and coverage["measured_runs"] == 2
    assert coverage["mean"] == 0.75 and coverage["with_unresolved"]["numerator"] == 1
    assert {row["group"] for row in body["rows"]} == {"fraud-case", "none"}
    # Nothing measured, and 0/0 rates, are reported as such: never 0 coverage, never a 100% pass rate.
    assert empty["totals"]["request_count"] == 0
    assert empty["totals"]["evidence_coverage"] == {
        "status": "not_measured",
        "measured_runs": 0,
        "mean": None,
        "with_unresolved": None,
        "provenance": None,
    }
    assert empty["totals"]["assurance"]["passed"]["value"] is None
    assert empty["totals"]["guard"]["passed"]["value"] is None


async def test_an_empty_selection_counts_under_its_own_kind(traffic, as_user, db) -> None:
    """Context Guard's non-empty-selection check: the CIM omitted every passage. Counted as `empty_selection`
    whether the row records failure codes or, as older rows do, the message only."""
    source = traffic.report.runs[0]
    day = datetime(2026, 2, 5, 9, tzinfo=UTC)
    message = "empty selection: all 3 evidence passages omitted"
    for index, details in enumerate(
        [{"failures": [message], "failure_codes": ["empty_selection"]}, {"failures": [message]}]
    ):
        clone = await _clone_run(db, source, f"run_quality_empty_{index:02d}", day, cim_state="invoked")
        db.add(
            ContextPackageRow(
                run_id=clone.run_id,
                cim_revision="test",
                context_profile="customer-notice",
                validation="failed-fallback-applied",
                guard_details=details,
                provenance="simulated",
            )
        )
    await db.commit()
    async with as_user("u-finance") as client:
        body = await _quality(client, **{"from": "2026-02-05", "to": "2026-02-05"})
    guard = body["totals"]["guard"]
    assert guard["passed"] == {"numerator": 0, "denominator": 2, "value": 0.0}
    assert guard["failures"] == [{"kind": "empty_selection", "count": 2}]


async def test_the_quality_report_keeps_the_reporting_scope(traffic, as_user, make_client) -> None:
    async with as_user("u-finance") as client:
        everything = await _quality(client, group_by="application")
    async with as_user("u-care-manager") as client:
        department = await _quality(client, group_by="application")
        outside = await client.get("/v1/reports/quality", params={**WINDOW, "department_id": "fraud-risk"})
        other_tenant = await client.get("/v1/reports/quality", params={**WINDOW, "tenant_id": "other"})
    assert {row["group"] for row in department["rows"]} == {"gct-support-chatbot"}
    support = next(row for row in everything["rows"] if row["group"] == "gct-support-chatbot")
    assert department["totals"]["request_count"] == support["request_count"]
    assert outside.status_code == other_tenant.status_code == 403
    token = await start_session(make_client)
    async with make_client(token) as client:
        visitor = await _quality(client)
        by_user = await client.get("/v1/reports/quality", params={**WINDOW, "user": "usr_0000000000000000"})
    assert visitor["metadata"]["scope"]["users_visible"] is False
    assert by_user.status_code == 403
    assert "usr_" not in str(visitor) and "principal" not in str(visitor)
