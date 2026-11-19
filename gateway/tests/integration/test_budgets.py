"""FinOps end to end (LLD §12, §21.9, §21.10, §22.10): budgets API, bill-shock caps under concurrency and
retries, exhaustion actions, alerts, cache-hit treatment, reconciliation and exact ledger totals.

Runs use the support chatbot's local-fast path with the opt-in frontier baseline: the local model is $0 and
the baseline is one Claude call whose worst case equals its actual cost when `max_output_tokens` is small,
so caps bind on real committed spend. Each test draws on its own workflow budget.
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, func, select, text

from aiva_gateway.core.errors import DependencyUnavailableError
from aiva_gateway.core.ids import ulid
from aiva_gateway.db.models import (
    AuditLogRow,
    BudgetAdjustmentRow,
    BudgetReservationRow,
    BudgetRow,
    ModelAttemptRow,
    UsageLedgerRow,
)
from aiva_gateway.finops.ledger import post_ledger_adjustment
from aiva_gateway.finops.quota import QuotaKeys, RedisQuotaService
from aiva_gateway.finops.reconcile import Reconciler
from aiva_gateway.finops.scopes import ILLUSTRATIVE, Bucket, period_bounds, scope_chain
from aiva_gateway.inference.registry import AdapterRegistry
from aiva_gateway.inference.simulated import SimulatedInferenceAdapter
from aiva_gateway.inference.tokens import input_token_bound
from aiva_gateway.pipeline.interfaces import (
    CachedResponse,
    CacheKey,
    InferenceRequest,
    InferenceResult,
    ReservationRequest,
    TokenUsage,
)
from tests.conftest import chat_body, console, with_overrides

APP = "gct-support-chatbot"
SHARED_CACHE_APP = "gct-knowledge-portal"


def workflow() -> str:
    return f"wf-{ulid()[-12:].lower()}"


def metered(wf: str, **extra: Any) -> dict[str, Any]:
    return chat_body(
        "What are the Europe roaming rates?",
        baseline_comparison=True,
        max_output_tokens=16,
        workflow_id=wf,
        **extra,
    )


def wf_bucket(wf: str, app: str = APP) -> Bucket:
    return Bucket("workflow", f"{app}/{wf}", "workflow", "lifetime")


@pytest.fixture
def finance(make_client, user_token):
    return lambda **kw: make_client(user_token("u-finance"), **kw)


async def set_budget(finance, wf: str, cap: int, *, app: str = APP, **extra: Any) -> dict[str, Any]:
    body = {
        "scope_type": "workflow",
        "scope_id": f"{app}/{wf}",
        "period": "workflow",
        "cap_micros": cap,
        **extra,
    }
    async with finance() as client:
        response = await client.post("/v1/admin/budgets", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def budget_status(finance, budget_id: str) -> dict[str, Any]:
    async with finance() as client:
        budgets = (await client.get("/v1/admin/budgets")).json()["budgets"]
    return next(b for b in budgets if b["id"] == budget_id)


async def ledger_total(db, run_ids: list[str], *, api_only: bool = True) -> int:
    statement = select(func.coalesce(func.sum(UsageLedgerRow.amount_micros), 0)).where(
        UsageLedgerRow.run_id.in_(run_ids)
    )
    if api_only:
        statement = statement.where(UsageLedgerRow.charge_type != "gpu_estimate")
    return int(await db.scalar(statement))


async def envelope_of_one_run(app_client, **kwargs: Any) -> tuple[int, int]:
    """(reserved, committed) of one metered run in an unbudgeted workflow."""
    async with app_client(APP, **kwargs) as client:
        body = (await client.post("/v1/ai/chat", json=metered(workflow()))).json()
    assert body["status"] == "completed", body
    return body["budget"]["reserved_micros"], body["budget"]["committed_micros"]


def reconciler(services, *, stale_after: timedelta = timedelta(0)) -> Reconciler:
    assert isinstance(services.quota, RedisQuotaService)
    return Reconciler(services.quota, services.sessionmaker, services.catalog, stale_after=stale_after)


class GatedAdapter(SimulatedInferenceAdapter):
    """Holds every call until released, so concurrent runs keep their reservations open together."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.waiting = 0

    async def complete(self, request: InferenceRequest) -> InferenceResult:
        self.waiting += 1
        await self.gate.wait()
        return await super().complete(request)


class WorstCaseAdapter(SimulatedInferenceAdapter):
    """Bills exactly what the reservation allows: the input-token bound and the full output allowance.

    Reservations size input with a conservative bound (inference/tokens.py), so an ordinary simulated
    run spends less than its envelope; runs at their worst case make thresholds land on exact runs.
    """

    async def complete(self, request: InferenceRequest) -> InferenceResult:
        result = await super().complete(request)
        usage = TokenUsage(
            input_tokens=input_token_bound(request.messages),
            output_tokens=request.max_output_tokens,
            provenance=result.usage.provenance,
        )
        return replace(result, usage=usage)


async def wait_for(condition) -> None:
    """Poll a condition on test-side state (the adapter and tasks expose no event to await)."""
    async with asyncio.timeout(5):
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


# --- Budgets API ---------------------------------------------------------------------------------------


async def test_finance_creates_updates_and_reads_budgets(finance, db) -> None:
    wf = workflow()
    created = await set_budget(finance, wf, 5_000_000)
    assert created["spent_micros"] == 0 and created["remaining_micros"] == 5_000_000
    assert created["period_key"] == "lifetime" and created["domain"] == "illustrative"
    assert created["provenance"] == "simulated", "illustrative figures are never labelled measured"
    updated = await set_budget(finance, wf, 7_000_000, alert_thresholds=[0.5, 0.9])
    assert updated["id"] == created["id"] and updated["alert_thresholds"] == [0.5, 0.9]
    actions = (
        await db.scalars(
            select(AuditLogRow.action).where(AuditLogRow.target_id == created["id"]).order_by(AuditLogRow.id)
        )
    ).all()
    assert actions == ["budget.create", "budget.update"]
    async with finance() as client:
        listed = (await client.get("/v1/admin/budgets")).json()
    tenant = next(b for b in listed["budgets"] if b["scope_type"] == "tenant")
    assert tenant["period_key"] == datetime.now(UTC).strftime("%Y-%m")
    assert (
        tenant["forecast_provenance"] == "estimated" and tenant["forecast_micros"] >= tenant["spent_micros"]
    )


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"scope_type": "application", "scope_id": APP, "period": "workflow", "cap_micros": 1}, 422),
        ({"scope_type": "application", "scope_id": "no-such-app", "period": "monthly", "cap_micros": 1}, 422),
        ({"scope_type": "tenant", "scope_id": "default", "period": "monthly", "cap_micros": 1}, 422),
        ({"scope_type": "gpu_pool", "scope_id": "no-pool", "period": "daily", "cap_micros": 1}, 422),
        (
            {
                "scope_type": "model",
                "scope_id": "claude-flagship",
                "period": "daily",
                "cap_micros": 1,
                "alert_thresholds": [1.2],
            },
            422,
        ),
        ({"scope_type": "workflow", "scope_id": f"{APP}/bad id", "period": "workflow", "cap_micros": 1}, 422),
    ],
)
async def test_budget_writes_are_validated(finance, body, status) -> None:
    async with finance() as client:
        response = await client.post("/v1/admin/budgets", json=body)
    assert response.status_code == status, response.text


async def test_budget_writes_require_finance_or_admin(make_client, user_token) -> None:
    body = {"scope_type": "application", "scope_id": APP, "period": "monthly", "cap_micros": 1}
    for user in ("u-care-manager", "u-support-owner", "u-tester", "u-auditor"):
        async with make_client(user_token(user)) as client:
            assert (await client.post("/v1/admin/budgets", json=body)).status_code == 403


async def test_user_budgets_are_keyed_by_pseudonym(finance) -> None:
    async with finance() as client:
        response = await client.post(
            "/v1/admin/budgets",
            json={"scope_type": "user", "scope_id": "u-tester", "period": "daily", "cap_micros": 10**9},
        )
    assert response.status_code == 201 and response.json()["scope_id"].startswith("usr_")


async def test_budget_reads_are_row_scoped(make_client, user_token) -> None:
    async def visible(user: str) -> set[tuple[str, str]]:
        async with make_client(user_token(user)) as client:
            response = await client.get("/v1/admin/budgets")
        assert response.status_code == 200
        return {(b["scope_type"], b["scope_id"]) for b in response.json()["budgets"]}

    everything = await visible("u-finance")
    assert ("tenant", "gct") in everything and ("application", "gct-coding-assistant") in everything
    care = await visible("u-care-manager")
    assert ("application", APP) in care
    assert not {s for s in care if s[0] == "tenant" or s[1] == "gct-coding-assistant"}
    owner = await visible("u-support-owner")
    assert owner and {s[1].split("/")[0] for s in owner} == {APP}
    async with make_client(user_token("u-tester")) as client:
        assert (await client.get("/v1/admin/budgets")).status_code == 403


# --- Reservations end to end ----------------------------------------------------------------------------


async def test_run_reserves_then_commits_actual_spend(app_client, finance, db) -> None:
    wf = workflow()
    budget = await set_budget(finance, wf, 10**9)
    async with app_client(APP) as client:
        body = (await client.post("/v1/ai/chat", json=metered(wf))).json()
    usage = body["budget"]
    # A simulated run's hold is illustrative, never labelled measured (ADR-0008 amendment).
    assert body["status"] == "completed" and usage["provenance"] == "simulated"
    assert usage["reserved_micros"] >= usage["committed_micros"] > 0
    assert usage["released_micros"] == usage["reserved_micros"] - usage["committed_micros"]
    status = await budget_status(finance, budget["id"])
    assert status["spent_micros"] == usage["committed_micros"] == await ledger_total(db, [body["run_id"]])
    assert status["reserved_micros"] == 0 and status["provenance"] == "simulated"
    journal = await db.get(BudgetReservationRow, usage["reservation_id"])
    assert journal is not None and journal.status == "settled"
    assert journal.allocations[wf_bucket(wf).key] == {
        "reserved": usage["reserved_micros"],
        "committed": usage["committed_micros"],
        "provenance": "simulated",
    }


async def test_concurrent_runs_can_never_exceed_the_hard_cap(app_client, services, finance, db) -> None:
    envelope, actual = await envelope_of_one_run(app_client)
    wf = workflow()
    cap = 2 * envelope + envelope // 2  # room for two worst-case runs in flight, never three
    budget = await set_budget(finance, wf, cap)
    adapter = GatedAdapter()
    gated = with_overrides(services, adapters=AdapterRegistry(services.catalog, adapters={}, default=adapter))
    async with app_client(APP, svc=gated) as client:
        tasks = [asyncio.create_task(client.post("/v1/ai/chat", json=metered(wf))) for _ in range(12)]
        await wait_for(lambda: adapter.waiting == 2 and sum(t.done() for t in tasks) == 10)
        held = await budget_status(finance, budget["id"])
        assert held["reserved_micros"] == 2 * envelope <= cap
        adapter.gate.set()
        bodies = [(await t).json() for t in tasks]
    completed = [b for b in bodies if b["status"] == "completed"]
    denied = [b for b in bodies if b["status"] == "blocked"]
    assert len(completed) == 2 and len(denied) == 10
    assert {b["error"]["code"] for b in denied} == {"QUOTA_EXCEEDED"}
    assert {tuple(b["error"]["rule_ids"]) for b in denied} == {(budget["id"],)}
    assert await ledger_total(db, [b["run_id"] for b in denied], api_only=False) == 0
    spent = await ledger_total(db, [b["run_id"] for b in completed])
    assert spent == 2 * actual <= cap
    # Committed spend now leaves less than one envelope: the cap binds on actual spend, not only holds.
    async with app_client(APP) as client:
        after = (await client.post("/v1/ai/chat", json=metered(wf))).json()
    assert after["status"] == ("blocked" if spent + envelope > cap else "completed")
    status = await budget_status(finance, budget["id"])
    assert status["reserved_micros"] == 0 and status["spent_micros"] <= cap


async def test_retries_with_one_idempotency_key_are_billed_once(app_client, services, finance, db) -> None:
    wf = workflow()
    budget = await set_budget(finance, wf, 10**9)
    adapter = GatedAdapter()
    gated = with_overrides(services, adapters=AdapterRegistry(services.catalog, adapters={}, default=adapter))
    headers = {"Idempotency-Key": f"retry-{ulid()}"}
    async with app_client(APP, svc=gated, headers=headers) as client:
        tasks = [asyncio.create_task(client.post("/v1/ai/chat", json=metered(wf))) for _ in range(6)]
        await wait_for(lambda: adapter.waiting == 1 and sum(t.done() for t in tasks) == 5)
        adapter.gate.set()
        responses = [await t for t in tasks]
        replay = await client.post("/v1/ai/chat", json=metered(wf))
    assert sorted(r.status_code for r in responses) == [200] + [409] * 5
    first = next(r.json() for r in responses if r.status_code == 200)
    assert replay.headers.get("Idempotent-Replayed") == "true" and replay.json()["run_id"] == first["run_id"]
    reservations = (
        await db.scalars(
            select(BudgetReservationRow.run_id).where(
                BudgetReservationRow.scope_chain.contains([["workflow", f"{APP}/{wf}"]])
            )
        )
    ).all()
    assert reservations == [first["run_id"]]
    status = await budget_status(finance, budget["id"])
    assert status["spent_micros"] == first["budget"]["committed_micros"]


async def test_thresholds_and_hard_cap_raise_alerts_from_real_runs(app_client, services, finance) -> None:
    worst = with_overrides(
        services, adapters=AdapterRegistry(services.catalog, adapters={}, default=WorstCaseAdapter())
    )
    envelope, actual = await envelope_of_one_run(app_client, svc=worst)
    assert actual == envelope, "the metered run's worst case is its actual cost"
    wf = workflow()
    budget = await set_budget(finance, wf, 20 * envelope)
    runs: list[dict[str, Any]] = []
    async with app_client(APP, svc=worst) as client:
        for _ in range(25):
            body = (await client.post("/v1/ai/chat", json=metered(wf))).json()
            runs.append(body)
            if body["status"] != "completed":
                break
    assert [r["status"] for r in runs] == ["completed"] * 20 + ["blocked"]
    async with finance() as client:
        alerts = (await client.get("/v1/admin/budgets/alerts")).json()["alerts"]
    mine = sorted((a for a in alerts if a["budget_id"] == budget["id"]), key=lambda a: a["created_at"])
    assert [(a["kind"], a["threshold"]) for a in mine] == [
        ("threshold", 0.8),
        ("threshold", 0.95),
        ("hard_cap", 1.0),
    ]
    assert [a["run_id"] for a in mine] == [runs[15]["run_id"], runs[18]["run_id"], runs[19]["run_id"]]
    assert all(a["provenance"] == "simulated" for a in mine)
    status = await budget_status(finance, budget["id"])
    assert status["thresholds_crossed"] == [0.8, 0.95, 1.0]
    assert status["spent_micros"] == status["cap_micros"] and status["remaining_micros"] == 0


async def test_alerts_are_row_scoped(make_client, user_token, finance) -> None:
    async with make_client(user_token("u-noc-manager")) as client:
        alerts = (await client.get("/v1/admin/budgets/alerts")).json()["alerts"]
    assert all(not (a["scope_id"] or "").startswith(APP) for a in alerts)


async def test_exhaustion_can_require_approval(app_client, finance, db) -> None:
    wf = workflow()
    budget = await set_budget(finance, wf, 0, on_exhaustion="approval_required")
    async with app_client(APP) as client:
        body = (await client.post("/v1/ai/chat", json=metered(wf))).json()
    assert body["status"] == "approval_required" and body["error"]["code"] == "QUOTA_EXCEEDED"
    assert body["error"]["rule_ids"] == [budget["id"]]
    # Govern's approval queue turns this into an approval request (its PR); FinOps only signals it.
    assert (
        await db.scalars(select(ModelAttemptRow).where(ModelAttemptRow.run_id == body["run_id"]))
    ).all() == []


async def test_lower_cost_route_stays_inside_the_eligible_set(tester_client, finance, db) -> None:
    wf = workflow()
    app = "gct-coding-assistant"
    budget = await set_budget(finance, wf, 0, app=app, on_exhaustion="lower_cost_route")
    payload = chat_body(
        "Refactor the billing loop.",
        task="coding",
        workflow_id=wf,
        routing={"mode": "hybrid", "preferred_model": "claude-flagship"},
        **console(app),
    )
    async with tester_client() as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed", body
    assert body["selected_model"] == "gct-qwen-finetuned" and body["execution_path"] == "cim-assisted"
    assert any(f"lower-cost route: budget {budget['id']}" in r for r in body["routing"]["reasons"])
    models = (
        await db.scalars(select(ModelAttemptRow.model_id).where(ModelAttemptRow.run_id == body["run_id"]))
    ).all()
    assert "claude-flagship" not in models and body["budget"]["committed_micros"] == 0


async def test_lower_cost_route_never_crosses_a_confidentiality_boundary(
    tester_client, services, finance, db
) -> None:
    async with finance() as client:
        response = await client.post(
            "/v1/admin/budgets",
            json={
                "scope_type": "gpu_pool",
                "scope_id": "gct-qwen-finetuned",
                "period": "daily",
                "cap_micros": 0,
                "on_exhaustion": "lower_cost_route",
            },
        )
    budget = response.json()
    try:
        payload = chat_body(
            "Summarise FR-9", data_classification="confidential", **console("gct-fraud-investigator")
        )
        async with tester_client() as client:
            body = (await client.post("/v1/ai/chat", json=payload)).json()
        assert body["status"] == "blocked" and body["error"]["code"] == "QUOTA_EXCEEDED"
        assert body["error"]["rule_ids"] == [budget["id"]]
        assert "No eligible lower-cost route" in body["error"]["message"]
        attempts = (
            await db.scalars(select(ModelAttemptRow).where(ModelAttemptRow.run_id == body["run_id"]))
        ).all()
        assert attempts == []
    finally:
        async with services.sessionmaker() as session, session.begin():
            await session.execute(delete(BudgetRow).where(BudgetRow.id == budget["id"]))
        await services.quota.publish_budgets("gct")


class DictCache:
    cache_type = "exact"

    def __init__(self) -> None:
        self.entries: dict[str, CachedResponse] = {}

    async def lookup(self, key: CacheKey) -> CachedResponse | None:
        return self.entries.get(key.key_hash)

    async def store(self, key: CacheKey, entry: CachedResponse, ttl_seconds: int) -> None:
        self.entries[key.key_hash] = entry


async def test_cache_hits_reserve_and_spend_nothing(app_client, services, finance) -> None:
    wf = workflow()
    budget = await set_budget(finance, wf, 10**9, app=SHARED_CACHE_APP)
    cached = with_overrides(services, cache=DictCache())
    # The support chatbot caches per end user (cache_sharing: per_user); app credentials name none.
    async with app_client(SHARED_CACHE_APP, svc=cached) as client:
        first = (await client.post("/v1/ai/chat", json=metered(wf))).json()
        second = (await client.post("/v1/ai/chat", json=metered(wf))).json()
    assert second["execution_path"] == "cache-return" and second["budget"]["reservation_id"] is None
    status = await budget_status(finance, budget["id"])
    assert status["spent_micros"] == first["budget"]["committed_micros"]


# --- Reconciliation --------------------------------------------------------------------------------------


class SettleFails(RedisQuotaService):
    async def settle(self, reservation, actual_micros, *, charges=()):
        raise DependencyUnavailableError("down", code="QUOTA_STORE_UNAVAILABLE")


async def test_reconciliation_settles_runs_whose_settlement_failed(app_client, services, finance, db) -> None:
    wf = workflow()
    budget = await set_budget(finance, wf, 10**9)
    flaky = with_overrides(
        services, quota=SettleFails(services.redis, services.sessionmaker, services.prices)
    )
    async with app_client(APP, svc=flaky) as client:
        body = (await client.post("/v1/ai/chat", json=metered(wf))).json()
    assert body["status"] == "completed"
    reservation_id = body["budget"]["reservation_id"]
    assert (await db.get(BudgetReservationRow, reservation_id)).status == "reserved"
    held = await budget_status(finance, budget["id"])
    assert held["reserved_micros"] == body["budget"]["reserved_micros"] and held["spent_micros"] == 0

    result = await reconciler(services).run("gct", triggered_by="test")
    assert result.status == "completed"
    assert any(
        f.kind == "settled_from_ledger" and f.reservation_id == reservation_id for f in result.findings
    )
    await db.rollback()
    assert (await db.get(BudgetReservationRow, reservation_id, populate_existing=True)).status == "settled"
    settled = await budget_status(finance, budget["id"])
    assert settled["reserved_micros"] == 0
    assert settled["spent_micros"] == await ledger_total(db, [body["run_id"]])


async def test_reconciliation_releases_orphaned_holds(services, finance, db) -> None:
    wf = workflow()
    budget = await set_budget(finance, wf, 10**9)
    chain = scope_chain(
        tenant_id="gct",
        department_id="customer-care",
        application_id=APP,
        principal_pseudonym=None,
        workflow_id=wf,
    )

    async def hold(amount: int) -> str:
        reservation = await services.quota.reserve(
            ReservationRequest(
                run_id=f"run_orphan{ulid()}",
                tenant_id="gct",
                scope_chain=chain,
                amount_micros=amount,
                idempotency_key=ulid(),
                domain=ILLUSTRATIVE,
            )
        )
        return reservation.reservation_id

    journaled = await hold(5_000)  # its run never recorded (the process died)
    redis_only = await hold(7_000)  # its journal row is gone (the write and the undo both failed)
    async with services.sessionmaker() as session, session.begin():
        await session.execute(
            delete(BudgetReservationRow).where(BudgetReservationRow.reservation_id == redis_only)
        )
    assert (await budget_status(finance, budget["id"]))["reserved_micros"] == 12_000

    result = await reconciler(services).run("gct", triggered_by="test")
    kinds = {(f.kind, f.reservation_id) for f in result.findings}
    assert ("expired_orphan", journaled) in kinds and ("released_redis_orphan", redis_only) in kinds
    status = await budget_status(finance, budget["id"])
    assert (status["reserved_micros"], status["spent_micros"]) == (0, 0)
    await db.rollback()
    assert (await db.get(BudgetReservationRow, journaled, populate_existing=True)).status == "expired"
    async with finance() as client:
        alerts = (await client.get("/v1/admin/budgets/alerts")).json()["alerts"]
    assert any(a["kind"] == "reservation_expired" and a["reservation_id"] == journaled for a in alerts)


async def test_ledger_corrections_flow_into_budgets_as_new_rows(app_client, services, finance, db) -> None:
    wf = workflow()
    budget = await set_budget(finance, wf, 10**9)
    async with app_client(APP) as client:
        body = (await client.post("/v1/ai/chat", json=metered(wf))).json()
    baseline = await db.scalar(
        select(ModelAttemptRow.attempt_id).where(
            ModelAttemptRow.run_id == body["run_id"], ModelAttemptRow.stage == "baseline"
        )
    )
    claude = Bucket("model", "claude-flagship", "monthly", datetime.now(UTC).strftime("%Y-%m"))
    before = (await services.quota.bucket_states("gct", [claude]))[claude.key].spent
    async with services.sessionmaker() as session, session.begin():
        await post_ledger_adjustment(
            session,
            services.catalog,
            run_id=body["run_id"],
            attempt_id=baseline,
            amount_micros=5_000,
            reason="provider invoice correction",
            actor_id="test",
        )
    result = await reconciler(services).run("gct", triggered_by="test")
    [finding] = [f for f in result.findings if f.kind == "ledger_adjustment"]
    assert finding.reservation_id == body["budget"]["reservation_id"] and finding.delta_micros == 5_000
    status = await budget_status(finance, budget["id"])
    assert (
        status["spent_micros"]
        == body["budget"]["committed_micros"] + 5_000
        == await ledger_total(db, [body["run_id"]])
    )
    assert (await services.quota.bucket_states("gct", [claude]))[claude.key].spent == before + 5_000
    rows = (
        await db.scalars(
            select(BudgetAdjustmentRow).where(BudgetAdjustmentRow.reservation_id == finding.reservation_id)
        )
    ).all()
    assert {r.bucket for r in rows} >= {wf_bucket(wf).key, claude.key} and {r.delta_micros for r in rows} == {
        5_000
    }
    again = await reconciler(services).run("gct", triggered_by="test")
    assert not [f for f in again.findings if f.kind == "ledger_adjustment"], "corrections are applied once"
    with pytest.raises(Exception, match="append-only"):
        await db.execute(
            text("update budget_adjustment set delta_micros = 0 where reservation_id = :r"),
            {"r": finding.reservation_id},
        )
    await db.rollback()


async def test_idle_counter_drift_is_corrected_from_the_ledger(app_client, services, finance, db) -> None:
    wf = workflow()
    budget = await set_budget(finance, wf, 10**9)
    async with app_client(APP) as client:
        body = (await client.post("/v1/ai/chat", json=metered(wf))).json()
    await services.redis.hset(QuotaKeys("gct").bucket(wf_bucket(wf).key), "spent", "1")
    result = await reconciler(services).run("gct", triggered_by="test")
    assert any(f.kind == "counter_corrected" and f.bucket == wf_bucket(wf).key for f in result.findings)
    status = await budget_status(finance, budget["id"])
    assert status["spent_micros"] == body["budget"]["committed_micros"]


async def test_reconcile_endpoint_requires_budget_write(make_client, user_token, finance) -> None:
    async with make_client(user_token("u-care-manager")) as client:
        assert (await client.post("/v1/admin/budgets/reconcile")).status_code == 403
    async with finance() as client:
        report = (await client.post("/v1/admin/budgets/reconcile")).json()
        listed = (await client.get("/v1/admin/budgets")).json()
    assert report["status"] == "completed" and report["triggered_by"].startswith("usr_")
    assert listed["last_reconciliation"]["reconciliation_id"] == report["reconciliation_id"]


# --- Exit gate: grouped totals reconcile exactly to the attempt-level ledger -------------------------------


async def test_grouped_totals_reconcile_exactly_to_the_ledger(services, finance, db) -> None:
    result = await reconciler(services).run("gct", triggered_by="test")
    assert result.status == "completed"
    now = datetime.now(UTC)
    start, _ = period_bounds("monthly", now) or (now, now)
    month = now.strftime("%Y-%m")

    async def ledger_by(group_sql: str) -> dict[str, int]:
        rows = await db.execute(
            text(
                f"""
                SELECT {group_sql} AS grp, SUM(l.amount_micros) AS total
                FROM usage_ledger l
                JOIN budget_reservation r ON r.reservation_id = l.reservation_id
                JOIN execution_run e ON e.run_id = l.run_id
                LEFT JOIN model_attempt a ON a.attempt_id = l.attempt_id
                WHERE l.tenant_id = 'gct' AND l.charge_type <> 'gpu_estimate'
                  AND r.allocations ? :tenant_bucket AND r.created_at >= :start
                GROUP BY 1
                """
            ),
            # Runs of this (illustrative) domain this month; live runs count in buckets of their own.
            {"start": start, "tenant_bucket": Bucket("tenant", "gct", "monthly", month).key},
        )
        return {str(g): int(t) for g, t in rows if g is not None}

    expected = {
        "tenant": await ledger_by("l.tenant_id"),
        "department": await ledger_by("e.department_id"),
        "application": await ledger_by("l.application_id"),
        "model": await ledger_by("a.model_id"),
    }
    buckets = [Bucket(scope, sid, "monthly", month) for scope, groups in expected.items() for sid in groups]
    live = await services.quota.bucket_states("gct", buckets)
    for bucket in buckets:
        assert live[bucket.key].spent == expected[bucket.scope_type][bucket.scope_id], bucket.key
        assert live[bucket.key].reserved == 0, bucket.key
    assert sum(expected["application"].values()) == sum(expected["tenant"].values()) > 0
    async with finance() as client:
        budgets = (await client.get("/v1/admin/budgets")).json()["budgets"]
    for budget in budgets:
        if budget["period"] == "monthly" and budget["scope_type"] in ("tenant", "application"):
            assert budget["spent_micros"] == expected[budget["scope_type"]].get(budget["scope_id"], 0), (
                budget["id"]
            )
