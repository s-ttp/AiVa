"""LLD §21.10 acceptance tests for reporting, automated (ADR-0015). Criterion -> test:

1. Synthetic traffic across >= 3 applications, several pseudonymous users, two languages (*), several task
   categories, sovereign-direct and CIM-assisted paths and a full response-cache hit:
   `test_synthetic_traffic_covers_the_acceptance_dimensions`.
2. Grouped token totals reconcile exactly to the attempt-level ledger; actual, estimated and avoided usage
   shown separately: `test_aggregation::test_hourly_totals_reconcile_exactly_to_the_attempt_ledger`,
   `test_reports_api::test_overview_reconciles_exactly_with_the_attempt_ledger`,
   `test_reports_api::test_simulated_usage_is_never_reported_as_measured`.
3. Drilldown application -> user -> category -> model -> trace; RBAC blocks cross-department and
   cross-tenant data: `test_reports_api::test_drilldown_from_application_to_trace`,
   `test_reports_api::test_department_manager_sees_only_their_department`.
4. Confidential request direct to GCT-Qwen, zero CIM and zero external attempts, attribution kept:
   `test_reports_api::test_confidential_request_is_sovereign_direct_with_attribution`.
5. Application budget threshold and hard cap with reservation, denial, alert, reconciliation and reporting:
   `test_application_budget_threshold_and_hard_cap`.
6. Classifier and taxonomy versions retained; ambiguous prompts unclassified:
   `test_reports_api::test_classification_keeps_versions_and_reports_unclassified`.
7. Filtered CSV export without prompt body, subscriber PII or unauthorized identities:
   `test_reports_api::test_filtered_csv_export_has_no_prompts_or_identities`.
8. Pinned frontier comparison with non-double-counted savings and visible quality pass/fail:
   `test_reports_api::test_pinned_frontier_comparison_shows_quality`,
   `test_reports_api::test_savings_waterfall_does_not_double_count`.

(*) English only this phase (ADR-0001): the language dimension is recorded on every run and reported, but
the traffic has one language. The two-language case is deferred to the Arabic phase.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from aiva_gateway.db.models import (
    BudgetAlertRow,
    BudgetReservationRow,
    ClassificationEventRow,
    ExecutionRunRow,
    UsageLedgerRow,
)
from aiva_gateway.finops.budgets import to_bps
from aiva_gateway.finops.quota import RedisQuotaService
from aiva_gateway.finops.reconcile import Reconciler
from tests.conftest import chat_body
from tests.insights.conftest import refresh_window

APP = "gct-incident-agent"
TODAY = datetime.now(UTC).date()
DAY_START = datetime.combine(TODAY, datetime.min.time(), tzinfo=UTC)
WINDOW = {"from": TODAY.isoformat(), "to": TODAY.isoformat()}


async def test_synthetic_traffic_covers_the_acceptance_dimensions(traffic, db) -> None:
    runs = list(
        await db.scalars(select(ExecutionRunRow).where(ExecutionRunRow.run_id.in_(traffic.report.runs)))
    )
    assert len(runs) == len(traffic.report.runs)
    assert len({r.application_id for r in runs}) >= 3
    assert len({r.principal_pseudonym for r in runs if r.principal_pseudonym}) >= 6
    assert {r.execution_path for r in runs} >= {
        "sovereign-direct",
        "cim-assisted",
        "cache-return",
        "local-fast",
    }
    assert {r.data_designation for r in runs} == {"synthetic"}
    events = list(
        await db.scalars(
            select(ClassificationEventRow).where(ClassificationEventRow.run_id.in_(traffic.report.runs))
        )
    )
    assert len({e.task_category for e in events if e.task_category}) >= 4
    assert {e.language for e in events} == {"english"}  # recorded on every run; English-only phase
    assert {"retry", "policy-denied", "unclassified", "baseline", "cache-hit"} <= set(traffic.report.by_label)


async def _budget(client, cap: int, thresholds: list[float]) -> dict:
    response = await client.post(
        "/v1/admin/budgets",
        json={
            "scope_type": "application",
            "scope_id": APP,
            "period": "daily",
            "cap_micros": cap,
            "alert_thresholds": thresholds,
            "on_exhaustion": "block",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_application_budget_threshold_and_hard_cap(
    services, app_client, make_client, user_token, db
) -> None:
    started = datetime.now(UTC)
    body = chat_body(
        "Summarise the synthetic incident INC-7781 timeline for the shift report.",
        task="analysis",
        baseline_comparison=True,
        max_output_tokens=16,
    )
    async with make_client(user_token("u-finance")) as finance:
        # A daily budget on the application, generous at first, to read today's spend before the test.
        status = await _budget(finance, 10_000_000_000, [0.8, 0.95])
        async with app_client(APP) as client:
            probe = (await client.post("/v1/ai/chat", json=body)).json()
        assert probe["status"] == "completed", probe
        held = await db.scalar(
            select(BudgetReservationRow).where(BudgetReservationRow.run_id == probe["run_id"])
        )
        assert held is not None and held.status == "settled"
        cost, envelope = held.actual_micros, held.estimate_micros
        assert 0 < cost <= envelope
        spent = (await finance.get("/v1/admin/budgets")).json()
        before = next(b for b in spent["budgets"] if b["id"] == status["id"])["spent_micros"]
        # Three more runs fit (each reserves the worst-case envelope first); the fourth is denied. The
        # thresholds sit between the second and third committed charge, and between the third and fourth.
        cap = before + 2 * cost + envelope
        thresholds = [(before + cost * 3 / 2) / cap, (before + cost * 5 / 2) / cap]
        await _budget(finance, cap, thresholds)

        outcomes = []
        async with app_client(APP) as client:
            for _ in range(4):
                outcomes.append((await client.post("/v1/ai/chat", json=body)).json())
        assert [o["status"] for o in outcomes] == ["completed", "completed", "completed", "blocked"]
        denied = outcomes[3]
        assert denied["outcome"] == "quota_denied"
        assert denied["error"]["code"] == "QUOTA_EXCEEDED" and denied["error"]["rule_ids"] == [status["id"]]

        # Reservation: every completed run reserved before it called a model, and settled after.
        for outcome in outcomes[:3]:
            row = await db.scalar(
                select(BudgetReservationRow).where(BudgetReservationRow.run_id == outcome["run_id"])
            )
            assert row is not None and row.status == "settled" and row.estimate_micros == envelope
            assert f"application|{APP}|daily|{TODAY.isoformat()}" in row.allocations
        assert (
            await db.scalar(select(func.count()).where(BudgetReservationRow.run_id == denied["run_id"])) == 0
        )

        # Alerts: both thresholds, then the hard cap on the denial.
        alerts = list(
            await db.scalars(
                select(BudgetAlertRow).where(
                    BudgetAlertRow.budget_id == status["id"], BudgetAlertRow.created_at >= started
                )
            )
        )
        thresholds_hit = {(a.threshold_bps, a.run_id) for a in alerts if a.kind == "threshold"}
        assert thresholds_hit == {
            (to_bps(thresholds[0]), outcomes[1]["run_id"]),
            (to_bps(thresholds[1]), outcomes[2]["run_id"]),
        }
        assert any(a.kind == "hard_cap" and a.run_id == denied["run_id"] for a in alerts)

        # Reconciliation: the journal, the live counters and the ledger agree.
        assert isinstance(services.quota, RedisQuotaService)
        reconciler = Reconciler(
            services.quota, services.sessionmaker, services.catalog, stale_after=timedelta(0)
        )
        result = await reconciler.run("gct", triggered_by="test")
        assert result.status == "completed", result.error
        budget = next(
            b for b in (await finance.get("/v1/admin/budgets")).json()["budgets"] if b["id"] == status["id"]
        )
        assert budget["spent_micros"] == before + 3 * cost
        assert budget["reserved_micros"] == 0
        assert budget["thresholds_crossed"] == [round(t, 4) for t in thresholds]

        # Reporting: the denial, the alerts and the spend appear in the Insights reports.
        await refresh_window(services, DAY_START, DAY_START + timedelta(days=1))
        governance = (
            await finance.get("/v1/reports/governance", params={**WINDOW, "group_by": "application"})
        ).json()
        overview = (
            await finance.get("/v1/reports/overview", params={**WINDOW, "application_id": APP})
        ).json()
        app_runs = select(ExecutionRunRow.run_id).where(
            ExecutionRunRow.application_id == APP, ExecutionRunRow.created_at >= DAY_START
        )
        ledger = await db.scalar(
            select(func.sum(UsageLedgerRow.amount_micros)).where(
                UsageLedgerRow.run_id.in_(app_runs), UsageLedgerRow.charge_type != "gpu_estimate"
            )
        )
        # Restore a generous cap so later tests on this application are unaffected.
        await _budget(finance, 10_000_000_000, [0.8, 0.95])
    row = next(r for r in governance["rows"] if r["group"] == APP)
    assert row["quota_denied"] >= 1
    kinds = {a["kind"]: a["count"] for a in governance["alerts"]}
    assert kinds.get("threshold", 0) >= 2 and kinds.get("hard_cap", 0) >= 1
    assert overview["usage"]["spend"]["api"]["total"] == ledger
    assert overview["outcomes"]["quota_denied"] >= 1
