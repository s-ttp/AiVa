"""Reporting APIs over synthetic traffic (LLD §21.6, §21.8, §21.10)."""

import asyncio
import csv
import io
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, inspect, select, update

from aiva_gateway.auth.principal import Caller
from aiva_gateway.contracts.reports import ExportRequest, IdentityResolutionRequest
from aiva_gateway.core.enums import Role
from aiva_gateway.core.errors import PermissionDeniedError
from aiva_gateway.db.models import (
    AuditLogRow,
    ExecutionRunRow,
    ModelAttemptRow,
    PrincipalRow,
    ReportExportRow,
    UsageLedgerRow,
)
from aiva_gateway.reporting import exports
from aiva_gateway.reporting.identity import resolve_identities
from aiva_gateway.reporting.scope import ReportScope, pseudonym_of
from aiva_gateway.reporting.traffic import CORE_PLAN, SYNTHETIC_USERS, plan_traffic, synthetic_pseudonym
from aiva_gateway.reporting.worker import sweep_exports
from tests.insights.conftest import refresh_window

TODAY = datetime.now(UTC).date()
WINDOW = {"from": TODAY.isoformat(), "to": TODAY.isoformat()}
DAY_START = datetime.combine(TODAY, datetime.min.time(), tzinfo=UTC)


@pytest.fixture
async def fresh(traffic, services):
    """Aggregates for the whole of today, including runs other tests wrote."""
    await refresh_window(services, DAY_START, DAY_START + timedelta(days=1))
    return traffic


@pytest.fixture
def as_user(make_client, user_token):
    return lambda user: make_client(user_token(user))


async def _get(client, path, **params):
    response = await client.get(path, params={**WINDOW, **params})
    assert response.status_code == 200, response.text
    return response.json()


async def _run(db, run_id: str) -> ExecutionRunRow:
    run = await db.get(ExecutionRunRow, run_id)
    assert run is not None
    return run


# --- Totals reconcile; actual, estimated and avoided stay separate ----------------------------------


async def test_overview_reconciles_exactly_with_the_attempt_ledger(fresh, as_user, db) -> None:
    async with as_user("u-finance") as client:
        body = await _get(client, "/v1/reports/overview")
    runs_today = select(ExecutionRunRow.run_id).where(
        ExecutionRunRow.tenant_id == "gct",
        ExecutionRunRow.created_at >= DAY_START,
        ExecutionRunRow.created_at < DAY_START + timedelta(days=1),
    )
    attempts = (
        await db.execute(
            select(func.sum(ModelAttemptRow.input_tokens), func.sum(ModelAttemptRow.output_tokens)).where(
                ModelAttemptRow.run_id.in_(runs_today)
            )
        )
    ).one()
    api = await db.scalar(
        select(func.sum(UsageLedgerRow.amount_micros)).where(
            UsageLedgerRow.run_id.in_(runs_today), UsageLedgerRow.charge_type != "gpu_estimate"
        )
    )
    gpu = await db.scalar(
        select(func.sum(UsageLedgerRow.amount_micros)).where(
            UsageLedgerRow.run_id.in_(runs_today), UsageLedgerRow.charge_type == "gpu_estimate"
        )
    )
    count = await db.scalar(select(func.count()).select_from(runs_today.subquery()))
    usage = body["usage"]
    tokens = usage["tokens"]
    assert usage["request_count"] == count
    assert tokens["input_tokens"]["total"] == attempts[0]
    assert tokens["output_tokens"]["total"] == attempts[1]
    assert usage["spend"]["api"]["total"] == (api or 0)
    assert usage["spend"]["gpu_estimated"]["value"] == (gpu or 0)
    # The token categories partition the totals: nothing counted twice.
    for kind in ("input", "output"):
        categories = ("upstream", "cim", "retry", "baseline", "quality_comparison", "other")
        parts = sum(tokens[f"{c}_{kind}_tokens"] for c in categories)
        assert parts == tokens[f"{kind}_tokens"]["total"]
    spend = usage["spend"]
    named = (
        "upstream_micros",
        "cim_micros",
        "retry_micros",
        "baseline_comparison_micros",
        "quality_comparison_micros",
        "adjustment_micros",
        "tool_call_micros",
    )
    assert sum(spend[k] for k in named) + spend["other_micros"] == spend["api"]["total"]


async def test_the_story_hero_figures_agree_with_the_rest_of_the_overview(fresh, as_user) -> None:
    """/week's hero figures (owner 2026-10-22) on the /live definitions, against the overview's own totals."""
    async with as_user("u-finance") as client:
        body = await _get(client, "/v1/reports/overview")
    kpis, usage = body["value_kpis"], body["usage"]
    cost, tokens, governance = kpis["cost"], kpis["tokens"], kpis["governance"]
    steps = {s["step"]: s["amount_micros"] for s in body["savings"]["steps"]}
    # The direct-frontier side is the waterfall's frontier baseline, on the same requests.
    assert cost["direct_frontier"]["value"] == steps["frontier_baseline"] > 0
    assert cost["saved"]["numerator"] == cost["direct_frontier"]["value"] - cost["aiva"]["value"]
    assert cost["hosting_excluded"] == usage["spend"]["gpu_estimated"]
    assert 0 < cost["priced_requests"] < usage["request_count"]  # the plan's denial is never priced
    assert tokens["fewer"]["numerator"] == tokens["caching_tokens"] + tokens["context_tokens"]
    assert tokens["fewer"]["denominator"] == tokens["base_tokens"] >= tokens["fewer"]["numerator"]
    assert governance["requests"] == usage["request_count"]
    outcomes = body["outcomes"]
    assert governance["stopped_by_policy"] == outcomes["policy_denied"] + outcomes["approval_required"] >= 1
    assert 1 <= governance["sensitive_on_sovereign"] <= governance["sensitive_requests"]
    # Simulated, synthetic traffic: never labelled measured or estimated.
    assert {k["provenance"] for k in (cost, tokens, governance)} <= {"simulated", "synthetic"}


async def test_simulated_usage_is_never_reported_as_measured(fresh, as_user, services) -> None:
    """The synthetic traffic's usage is simulated, never measured, and labelled synthetic. Scoped to the
    traffic's own principals: other tests' runs and ledger corrections in the same hours (e.g. a measured
    adjustment in integration/test_budgets.py) must not change the outcome, whatever the test order."""
    traffic_users = {synthetic_pseudonym(services, user) for user in SYNTHETIC_USERS}
    async with as_user("u-finance") as client:
        body = await _get(client, "/v1/reports/consumption", group_by="user", limit=500)
    rows = [row["usage"] for row in body["rows"] if row["group"] in traffic_users]
    assert len(rows) >= len(traffic_users) // 2
    for usage in rows:
        assert usage["provenance"] == "synthetic"
        tokens, spend = usage["tokens"], usage["spend"]
        for split in (tokens["input_tokens"], tokens["output_tokens"], spend["api"]):
            assert split["measured"] == 0
            assert split["simulated"] == split["total"]
            if split["total"]:
                assert split["provenance"] == "simulated"
        assert spend["gpu_estimated"]["provenance"] in ("estimated", "simulated", "synthetic")
        assert usage["avoided"]["provenance"] != "measured" and usage["avoided"]["micros"] >= 0
    assert sum(usage["avoided"]["calls"] for usage in rows) >= 1  # the plan's full response-cache hit


async def test_savings_waterfall_does_not_double_count(fresh, as_user) -> None:
    async with as_user("u-finance") as client:
        body = await _get(client, "/v1/reports/overview")
    steps = {s["step"]: s["amount_micros"] for s in body["savings"]["steps"]}
    assert sum(v for k, v in steps.items() if k != "net") == steps["net"]
    spend = body["usage"]["spend"]
    measurement = body["savings"]["measurement_cost"]
    # The net is the cost of serving; the opt-in baseline and answer-quality comparisons come after it as a
    # measurement cost, never reducing the savings, and the two together are all actual API spend plus the
    # local GPU estimate.
    assert "baseline_comparison" not in steps
    assert measurement["value"] == spend["baseline_comparison_micros"] + spend["quality_comparison_micros"]
    assert spend["baseline_comparison_micros"] > 0
    assert steps["net"] + measurement["value"] == spend["api"]["total"] + spend["gpu_estimated"]["value"]
    assert body["savings"]["net_savings_micros"] == steps["frontier_baseline"] - steps["net"]
    assert body["savings"]["quality_adjusted"] is False
    assert {s["provenance"] for s in body["savings"]["steps"]} <= {"estimated", "simulated", "synthetic"}


async def test_pinned_frontier_comparison_shows_quality(fresh, as_user, services) -> None:
    async with as_user("u-finance") as client:
        baseline = (await _get(client, "/v1/reports/overview"))["baseline"]
    frontier = services.catalog.model("claude-flagship")
    assert frontier is not None
    assert baseline["frontier_model_id"] == "claude-flagship"
    assert baseline["frontier_model_name"] == frontier.display_name
    assert baseline["runs"] >= 2
    assert baseline["quality_passed_runs"] + baseline["quality_failed_runs"] == baseline["runs"]
    assert baseline["savings"]["value"] == baseline["baseline"]["value"] - baseline["actual"]["value"]
    assert baseline["baseline"]["provenance"] == "simulated"
    assert baseline["baseline_quality"] == "not_graded"


# --- Drilldown: application -> user -> category -> model -> trace -------------------------------------


async def test_drilldown_from_application_to_trace(fresh, as_user) -> None:
    async with as_user("u-tenant-admin") as client:
        apps = await _get(client, "/v1/reports/consumption", group_by="application")
        assert len([r for r in apps["rows"] if r["usage"]["request_count"]]) >= 3
        support = next(r for r in apps["rows"] if r["group"] == "gct-support-chatbot")
        assert support["label"] == "Customer Support Chatbot"

        app_filter = {"application_id": "gct-support-chatbot"}
        users = await _get(client, "/v1/reports/consumption", group_by="user", **app_filter)
        pseudonymous = [r for r in users["rows"] if r["group"].startswith("usr_")]
        assert len(pseudonymous) >= 2
        assert sum(r["usage"]["request_count"] for r in users["rows"]) == support["usage"]["request_count"]
        user = max(pseudonymous, key=lambda r: r["usage"]["request_count"])["group"]

        user_filter = {**app_filter, "user": user}
        categories = await _get(client, "/v1/reports/consumption", group_by="category", **user_filter)
        category = categories["rows"][0]["group"]
        models = await _get(
            client, "/v1/reports/consumption", group_by="model", task_category=category, **user_filter
        )
        served = next(r for r in models["rows"] if r["usage"]["request_count"])
        runs = await _get(
            client,
            "/v1/reports/runs",
            task_category=category,
            model_id=served["group"] or "",
            **user_filter,
        )
        assert runs["page"]["total_rows"] >= served["usage"]["request_count"] >= 1
        trace = runs["runs"][0]
        assert trace["principal"] == user and trace["application_id"] == "gct-support-chatbot"
        detail = await _get(client, f"/v1/reports/runs/{trace['trace_id']}")
    assert detail["run"]["run_id"] == trace["run_id"]
    assert detail["usage"]["request_count"] == 1
    assert (
        sum(a["input_tokens"] for a in detail["attempts"])
        == detail["usage"]["tokens"]["input_tokens"]["total"]
    )
    assert detail["spans"] and all("prompt" not in s["name"] for s in detail["spans"])


async def test_run_counts_match_aggregate_counts(fresh, as_user) -> None:
    async with as_user("u-finance") as client:
        users = await _get(
            client, "/v1/reports/consumption", group_by="user", application_id="gct-noc-assistant"
        )
        for row in users["rows"]:
            runs = await _get(
                client, "/v1/reports/runs", application_id="gct-noc-assistant", user=row["group"], limit=1
            )
            assert runs["page"]["total_rows"] == row["usage"]["request_count"]


async def test_consumption_pages_with_a_cursor(fresh, as_user) -> None:
    async with as_user("u-finance") as client:
        first = await _get(client, "/v1/reports/consumption", group_by="application", limit=2)
        assert first["page"]["next_cursor"]
        second = await _get(
            client,
            "/v1/reports/consumption",
            group_by="application",
            limit=2,
            cursor=first["page"]["next_cursor"],
        )
        bad = await client.get(
            "/v1/reports/consumption", params={**WINDOW, "group_by": "model", "cursor": "zz"}
        )
    assert {r["group"] for r in first["rows"]}.isdisjoint({r["group"] for r in second["rows"]})
    assert first["page"]["total_rows"] == second["page"]["total_rows"]
    assert bad.status_code == 422


# --- RBAC: tenant and department scoping ------------------------------------------------------------


async def test_department_manager_sees_only_their_department(fresh, as_user, db) -> None:
    fraud_run = await _run(db, fresh.report.by_label["sovereign-direct"][0])
    async with as_user("u-care-manager") as client:
        apps = await _get(client, "/v1/reports/consumption", group_by="application")
        users = await _get(client, "/v1/reports/consumption", group_by="user")
        overview = await _get(client, "/v1/reports/overview")
        other_department = await client.get(
            "/v1/reports/consumption", params={**WINDOW, "group_by": "user", "department_id": "fraud-risk"}
        )
        other_app = await client.get(
            "/v1/reports/consumption",
            params={**WINDOW, "group_by": "user", "application_id": "gct-fraud-investigator"},
        )
        detail = await client.get(f"/v1/reports/runs/{fraud_run.trace_id}")
        other_tenant = await client.get("/v1/reports/overview", params={**WINDOW, "tenant_id": "other-telco"})
        runs = await _get(client, "/v1/reports/runs", limit=500)
    assert {r["group"] for r in apps["rows"]} == {"gct-support-chatbot"}
    assert overview["metadata"]["scope"] == {
        "level": "department",
        "department_id": "customer-care",
        "application_ids": ["gct-public-web-assistant", "gct-support-chatbot"],
        "user_identities": "pseudonymous",
        "users_visible": True,
    }
    assert all(r["group"].startswith(("usr_", "service:gct-support-chatbot")) for r in users["rows"])
    assert other_department.status_code == 403 and other_app.status_code == 403
    assert detail.status_code == 404
    assert other_tenant.status_code == 403
    assert {r["application_id"] for r in runs["runs"]} == {"gct-support-chatbot"}


async def test_application_owner_manager_and_tester_scopes(fresh, as_user) -> None:
    async with as_user("u-support-owner") as client:
        apps = await _get(client, "/v1/reports/consumption", group_by="application")
    async with as_user("u-tester") as client:
        tester = await _get(client, "/v1/reports/overview")
    async with as_user("u-noc-manager") as client:
        noc = await _get(client, "/v1/reports/consumption", group_by="application")
    assert {r["group"] for r in apps["rows"]} == {"gct-support-chatbot"}
    # The presenter (ADR-0021 amendment): the tenant, without the user dimension (test_tester_reports.py).
    assert tester["metadata"]["scope"]["level"] == "tenant"
    assert tester["metadata"]["scope"]["users_visible"] is False
    assert {r["group"] for r in noc["rows"]} <= {"gct-noc-assistant", "gct-incident-agent"}


# --- Confidential routing keeps attribution ---------------------------------------------------------


async def test_confidential_request_is_sovereign_direct_with_attribution(fresh, as_user, db) -> None:
    run = await _run(db, fresh.report.by_label["sovereign-direct"][0])
    async with as_user("u-auditor") as client:
        detail = await _get(client, f"/v1/reports/runs/{run.trace_id}")
        governance = await _get(client, "/v1/reports/governance", group_by="application")
        consumption = await _get(
            client, "/v1/reports/consumption", group_by="user", application_id="gct-fraud-investigator"
        )
    assert detail["run"]["execution_path"] == "sovereign-direct"
    assert detail["run"]["effective_classification"] == "confidential"
    assert detail["run"]["cim_state"] == "bypassed-policy"
    assert [a["model_id"] for a in detail["attempts"]] == ["gct-qwen-finetuned"]
    assert not [a for a in detail["attempts"] if a["stage"] == "cim"]
    assert detail["run"]["principal"].startswith("usr_")
    fraud = next(r for r in governance["rows"] if r["group"] == "gct-fraud-investigator")
    assert fraud["confidential_requests"] == fraud["request_count"] >= 1
    assert fraud["sovereign_direct"] >= 1
    assert governance["totals"]["confidential_external_attempts"] == 0
    assert governance["totals"]["confidential_cim_attempts"] == 0
    assert sum(r["usage"]["request_count"] for r in consumption["rows"]) == fraud["request_count"]


async def test_policy_denial_is_reported_with_its_rule(fresh, as_user) -> None:
    async with as_user("u-auditor") as client:
        governance = await _get(client, "/v1/reports/governance", group_by="application")
    portal = next(r for r in governance["rows"] if r["group"] == "gct-knowledge-portal")
    assert portal["policy_denied"] >= 1
    assert portal["denial_reasons"] and all(r["rule_id"] for r in portal["denial_reasons"])


# --- Classification versions and unclassified -------------------------------------------------------


async def test_classification_keeps_versions_and_reports_unclassified(fresh, as_user, db) -> None:
    run = await _run(db, fresh.report.by_label["unclassified"][0])
    async with as_user("u-finance") as client:
        report = await _get(client, "/v1/reports/classification", group_by="task_category")
        languages = await _get(client, "/v1/reports/classification", group_by="language")
        detail = await _get(client, f"/v1/reports/runs/{run.trace_id}")
    assert "tax-2026.10.1" in report["metadata"]["taxonomy_versions"]
    assert "heuristic-v0" in report["metadata"]["classifier_versions"]
    assert report["taxonomy"] == {"version": "tax-2026.10.1", "review_confidence_threshold": 0.6}
    unclassified = next(r for r in report["rows"] if r["group"] == "unclassified")
    assert unclassified["low_confidence"] is True and unclassified["request_count"] >= 1
    assert report["unclassified"]["numerator"] >= 1
    assert {r["group"] for r in report["rows"]} >= {"qa", "analysis", "coding"}
    # English only this phase (ADR-0001); the language dimension is still recorded.
    assert {r["group"] for r in languages["rows"]} == {"english"}
    classification = detail["classification"]
    assert classification["status"] == "unclassified"
    assert classification["reported_task_category"] == "unclassified"
    assert classification["predicted_task_category"] == "qa"
    assert classification["taxonomy_version"] == "tax-2026.10.1"


# --- Identity resolution ----------------------------------------------------------------------------


async def test_identity_resolution_is_permissioned_and_audited(fresh, as_user, db) -> None:
    run = await _run(db, fresh.report.runs[0])
    body = {
        "pseudonyms": [run.principal_pseudonym, "usr_0000000000000000"],
        "reason": "Investigating a spike",
    }
    async with as_user("u-finance") as client:
        denied = await client.post("/v1/reports/identities/resolve", json=body)
    async with as_user("u-auditor") as client:
        resolved = await client.post("/v1/reports/identities/resolve", json=body)
    assert denied.status_code == 403
    assert resolved.status_code == 200, resolved.text
    identities = resolved.json()["identities"]
    assert identities[0]["kind"] == "synthetic" and identities[0]["user_id"].startswith("syn-")
    assert identities[1]["kind"] == "unknown"
    audit = await db.scalar(
        select(AuditLogRow)
        .where(AuditLogRow.action == "report.identity_resolve")
        .order_by(AuditLogRow.id.desc())
        .limit(1)
    )
    assert audit is not None and audit.details["pseudonyms"] == body["pseudonyms"]
    assert audit.details["reason"] == body["reason"]


async def test_a_user_run_without_a_pseudonym_is_unattributed_never_identified(fresh, as_user, db, services):
    """A user run recorded without a pseudonym is reported as unattributed: not as a user ID, and not as
    the application's service principal (ADR-0015)."""
    source = await _run(db, fresh.report.runs[0])
    values = {attr.key: getattr(source, attr.key) for attr in inspect(ExecutionRunRow).mapper.column_attrs}
    hour = datetime(2026, 1, 5, 10, tzinfo=UTC)
    values.update(
        run_id="run_unattributed_test",
        trace_id="0" * 31 + "a",
        principal_kind="user",
        principal_pseudonym=None,
        created_at=hour,
        completed_at=hour,
    )
    db.add(ExecutionRunRow(**values))
    await db.commit()
    await refresh_window(services, hour, hour + timedelta(hours=1))
    window = {"from": "2026-01-05", "to": "2026-01-05"}
    async with as_user("u-finance") as client:
        users = (await client.get("/v1/reports/consumption", params={**window, "group_by": "user"})).json()
        runs = (await client.get("/v1/reports/runs", params=window)).json()
        detail = (await client.get(f"/v1/reports/runs/{values['trace_id']}")).json()
        job = await _export(client, report="consumption", group_by="user", window=window)
        csv = (await client.get(job["download_url"])).text
    assert [(row["group"], row["label"]) for row in users["rows"]] == [("unattributed", "Unattributed user")]
    assert [run["principal"] for run in runs["runs"]] == ["unattributed"]
    assert detail["run"]["principal"] == "unattributed"
    assert "unattributed" in csv
    for text in (str(users), str(runs), str(detail), csv):
        assert "service:" not in text and not re.search(r"\b(?:u|syn)-[a-z]+", text)


async def test_a_caller_without_a_pseudonym_is_refused_before_anything_is_written(db, services) -> None:
    """Exports and identity resolution record callers by pseudonym only; a user without one is refused,
    and its user ID reaches neither report_export nor audit_log (ADR-0015)."""
    user = Caller(kind="user", tenant_id="gct", user_id="u-finance", pseudonym="usr_0123456789abcdef")
    assert pseudonym_of(user) == "usr_0123456789abcdef"
    nameless = Caller(
        kind="user", tenant_id="gct", user_id="u-no-pseudonym", roles=frozenset({Role.TENANT_ADMIN})
    )
    scope = ReportScope("gct", tenant_wide=True, department_id=None, application_ids=frozenset())
    export = ExportRequest.model_validate({"report": "governance", "group_by": "policy", "window": WINDOW})
    resolve = IdentityResolutionRequest(pseudonyms=["usr_0000000000000000"], reason="Investigating a spike")
    async with services.sessionmaker() as session:
        with pytest.raises(PermissionDeniedError):
            await exports.create_export(session, services.catalog, scope, nameless, export, ttl_seconds=60)
        with pytest.raises(PermissionDeniedError):
            await resolve_identities(session, services.settings, services.catalog, nameless, resolve)
        await session.commit()
    written = await db.scalar(
        select(func.count()).select_from(AuditLogRow).where(AuditLogRow.actor_id == nameless.user_id)
    )
    exported = await db.scalar(
        select(func.count())
        .select_from(ReportExportRow)
        .where(ReportExportRow.requester_pseudonym == nameless.user_id)
    )
    assert written == exported == 0


# --- CSV export -------------------------------------------------------------------------------------


async def _export(client, **body):
    response = await client.post("/v1/reports/exports", json=body)
    assert response.status_code == 202, response.text
    job = response.json()
    for _ in range(100):
        job = (await client.get(f"/v1/reports/exports/{job['export_id']}")).json()
        if job["status"] not in ("queued", "running"):
            break
        await asyncio.sleep(0.05)
    return job


async def test_filtered_csv_export_has_no_prompts_or_identities(fresh, as_user, db, services) -> None:
    request = {
        "report": "consumption",
        "group_by": "user",
        "window": WINDOW,
        "filters": {"application_id": "gct-support-chatbot"},
    }
    async with as_user("u-finance") as client:
        job = await _export(client, **request)
        assert job["status"] == "completed", job
        download = await client.get(job["download_url"])
        listed = (await client.get("/v1/reports/exports")).json()["exports"]
    async with as_user("u-auditor") as client:
        stranger = await client.get(job["download_url"])
    async with as_user("u-care-manager") as client:
        manager = await client.post("/v1/reports/exports", json=request)
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("text/csv")
    assert download.headers["cache-control"] == "no-store"
    assert any(j["export_id"] == job["export_id"] for j in listed)
    assert stranger.status_code == 404 and manager.status_code == 403
    text = download.text
    for line in (
        "# Data designation: synthetic",
        "# Timezone: UTC",
        "# Filters: application_id=gct-support-chatbot",
        f"# Pricing snapshot: {services.catalog.rate_cards.version}",  # from config/rate_cards.yaml
        "# Taxonomy versions: tax-2026.10.1",
        "# Users: pseudonymous",
    ):
        assert line in text
    assert f"# Period: {TODAY.isoformat()} to {TODAY.isoformat()} (inclusive)" in text
    assert "usr_" in text
    prompts = {spec.content for spec in plan_traffic(40, seed=11)} | {spec.content for spec in CORE_PLAN}
    for prompt in prompts:
        assert prompt not in text
    for leaked in ("syn-care", "u-tester", "AiVa Tester", "Synthetic traffic user", "MSISDN"):
        assert leaked not in text
    assert not re.search(r"\+?9665\d{8}", text)  # no subscriber numbers
    actions = set(
        await db.scalars(
            select(AuditLogRow.action).where(
                AuditLogRow.target_type == "report_export", AuditLogRow.target_id == job["export_id"]
            )
        )
    )
    assert actions == {"report.export.request", "report.export.download"}


async def test_export_expires_and_is_purged(fresh, as_user, db, services) -> None:
    async with as_user("u-finance") as client:
        job = await _export(client, report="governance", group_by="policy", window=WINDOW)
        assert job["status"] == "completed"
        async with services.sessionmaker() as session, session.begin():
            await session.execute(
                update(ReportExportRow)
                .where(ReportExportRow.export_id == job["export_id"])
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        expired = await client.get(job["download_url"])
        status = (await client.get(f"/v1/reports/exports/{job['export_id']}")).json()
    assert expired.status_code == 410
    assert status["status"] == "expired" and status["download_url"] is None
    async with services.sessionmaker() as session:
        row = await session.get(ReportExportRow, job["export_id"])
    assert row is not None and row.content is None


def test_a_scope_covers_only_rows_it_still_sees() -> None:
    tenant = ReportScope("gct", tenant_wide=True, department_id=None, application_ids=frozenset())
    care = ReportScope("gct", False, "customer-care", frozenset({"gct-support-chatbot"}))
    owner = ReportScope("gct", False, None, frozenset({"gct-support-chatbot"}))
    other_tenant = ReportScope("other", True, None, frozenset())
    assert tenant.covers(care) and care.covers(owner) and owner.covers(owner)
    assert not owner.covers(care) and not care.covers(tenant) and not tenant.covers(other_tenant)


async def test_export_download_rechecks_the_caller(fresh, as_user, db, services) -> None:
    """A role revoked within the export's lifetime stops the download, and so does a scope that no longer
    covers the export's recorded scope; both are refused with 403."""
    async with as_user("u-auditor") as client:
        job = await _export(client, report="governance", group_by="policy", window=WINDOW)
        assert job["status"] == "completed"
        principal = await db.get(PrincipalRow, "u-auditor")
        assert principal is not None
        roles = list(principal.roles)
        try:
            principal.roles = ["department_manager"]
            await db.commit()
            revoked = await client.get(job["download_url"])
        finally:
            principal.roles = roles
            await db.commit()
        allowed = await client.get(job["download_url"])
    assert revoked.status_code == 403 and allowed.status_code == 200

    row = await db.get(ReportExportRow, job["export_id"])
    assert row is not None and row.requester_pseudonym.startswith("usr_")
    caller = Caller(kind="user", tenant_id="gct", user_id="u-auditor", pseudonym=row.requester_pseudonym)
    narrowed = ReportScope("gct", tenant_wide=False, department_id="finance", application_ids=frozenset())
    async with services.sessionmaker() as session:
        with pytest.raises(PermissionDeniedError):
            await exports.download(session, caller, job["export_id"], scope=narrowed)
        await session.commit()
    refused = await db.scalar(
        select(func.count()).where(
            AuditLogRow.action == "report.export.download_refused", AuditLogRow.target_id == job["export_id"]
        )
    )
    assert refused == 1


async def test_optimization_csv_carries_the_savings_waterfall(fresh, as_user) -> None:
    """Every waterfall step with its provenance, then the measurement cost; the steps sum to the net cost
    and match the optimization report the API returns."""
    async with as_user("u-finance") as client:
        report = await _get(client, "/v1/reports/optimization", group_by="application")
        job = await _export(client, report="optimization", group_by="application", window=WINDOW)
        text = (await client.get(job["download_url"])).text
    table = list(
        csv.DictReader(io.StringIO("\n".join(line for line in text.splitlines() if not line.startswith("#"))))
    )
    assert table and set(table[0]) >= {
        f"waterfall_{s}_{k}" for s in exports.SAVINGS_STEPS for k in ("micros", "provenance")
    }
    assert "baseline_comparison" not in exports.SAVINGS_STEPS
    expected = {row["group"]: row["savings"] for row in report["rows"]}
    for line in table:
        steps = {s: int(line[f"waterfall_{s}_micros"]) for s in exports.SAVINGS_STEPS}
        assert sum(v for s, v in steps.items() if s != "net") == steps["net"]
        assert int(line["estimated_net_savings_micros"]) == steps["frontier_baseline"] - steps["net"]
        api = expected[line["group"]]
        assert steps == {step["step"]: step["amount_micros"] for step in api["steps"]}
        assert {s: line[f"waterfall_{s}_provenance"] for s in exports.SAVINGS_STEPS} == {
            step["step"]: step["provenance"] for step in api["steps"]
        }
        measurement = api["measurement_cost"]
        assert int(line["measurement_cost_micros"]) == measurement["value"]
        assert line["measurement_cost_provenance"] == measurement["provenance"]


def _in_flight_export(export_id: str, created_at: datetime, status: str = "running") -> ReportExportRow:
    return ReportExportRow(
        export_id=export_id,
        tenant_id="gct",
        requester_pseudonym="usr_0123456789abcdef",
        report="governance",
        group_by="policy",
        window_from=TODAY,
        window_to=TODAY,
        filters={},
        scope={"tenant_id": "gct", "tenant_wide": True, "department_id": None, "application_ids": []},
        status=status,
        created_at=created_at,
        expires_at=created_at + timedelta(hours=1),
    )


async def test_exports_orphaned_by_a_restart_are_recorded_failed_and_audited(db, services) -> None:
    """Exports render in a task of the gateway process; a restart orphans them. The worker fails what was in
    flight before it started, then anything in flight longer than STALE_AFTER, and audits each."""
    now = datetime.now(UTC)
    db.add_all(
        [
            _in_flight_export("exp_orphaned_long_ago", now - exports.STALE_AFTER - timedelta(minutes=1)),
            _in_flight_export("exp_orphaned_at_restart", now - timedelta(seconds=30), status="queued"),
        ]
    )
    await db.commit()

    async def status_of(export_id: str) -> tuple[str, str | None]:
        async with services.sessionmaker() as session:
            row = await session.get(ReportExportRow, export_id)
            assert row is not None
            return row.status, row.error

    # Each interval: only exports in flight for longer than STALE_AFTER.
    await sweep_exports(services, ["gct"], created_before=now - exports.STALE_AFTER)
    assert await status_of("exp_orphaned_long_ago") == ("failed", "interrupted")
    assert await status_of("exp_orphaned_at_restart") == ("queued", None)
    # At worker start: everything created before the process started.
    await sweep_exports(services, ["gct"], created_before=now)
    assert await status_of("exp_orphaned_at_restart") == ("failed", "interrupted")
    # A late runner never revives a job recorded as failed.
    await exports.run_export(
        services.sessionmaker, services.catalog, "exp_orphaned_at_restart", pricing_snapshot="v"
    )
    assert await status_of("exp_orphaned_at_restart") == ("failed", "interrupted")
    audited = list(
        await db.scalars(
            select(AuditLogRow.target_id).where(
                AuditLogRow.action == "report.export.interrupted", AuditLogRow.actor_kind == "system"
            )
        )
    )
    assert sorted(audited) == ["exp_orphaned_at_restart", "exp_orphaned_long_ago"]


async def test_export_validates_group_and_scope(fresh, as_user) -> None:
    async with as_user("u-finance") as client:
        wrong_group = await client.post(
            "/v1/reports/exports", json={"report": "governance", "group_by": "user", "window": WINDOW}
        )
    assert wrong_group.status_code == 422


async def test_filter_options_follow_the_reporting_scope(fresh, as_user) -> None:
    async with as_user("u-finance") as client:
        everything = await _get(client, "/v1/reports/filters")
    async with as_user("u-care-manager") as client:
        department = await _get(client, "/v1/reports/filters")
    apps = {o["value"]: o["label"] for o in everything["options"]["application_id"]}
    assert apps["gct-fraud-investigator"] == "Fraud Investigation Assistant" and len(apps) >= 3
    assert "sovereign-direct" in {o["value"] for o in everything["options"]["execution_path"]}
    assert {o["value"] for o in department["options"]["application_id"]} == {"gct-support-chatbot"}
    assert {o["value"] for o in department["options"]["department_id"]} == {"customer-care"}
    assert all(
        o["value"].startswith(("usr_", "service:gct-support-chatbot")) for o in department["options"]["user"]
    )
