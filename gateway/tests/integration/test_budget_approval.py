"""Approved budget exceptions (ADR-0014): a `budget` approval lets one resumed request reserve past the caps
it names, by at most the approved amount, once. Anything else fails closed to the ordinary caps.

Each test draws on its own zero-cap workflow budget with `on_exhaustion: approval_required`, so the first
request becomes a budget approval and only an applied exception can admit the resumed one.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, func, select, update

from aiva_gateway.core.errors import ConflictError
from aiva_gateway.core.ids import ulid
from aiva_gateway.db.models import (
    ApprovalRequestRow,
    AuditLogRow,
    BudgetAlertRow,
    BudgetReservationRow,
    BudgetRow,
    ModelAttemptRow,
)
from aiva_gateway.finops.approvals import ALREADY_USED, ApprovedException
from aiva_gateway.finops.quota import QuotaKeys, RedisQuotaService, _Attempt
from aiva_gateway.finops.scopes import ILLUSTRATIVE, Bucket, period_key, scope_chain
from aiva_gateway.pipeline.interfaces import QuotaDeniedError, Reservation, ReservationRequest
from tests.conftest import chat_body, console
from tests.integration.test_budgets import APP, metered, set_budget, wf_bucket, workflow

EXTERNAL = {"gemini-flash-lite", "deepseek-v4-1-flash", "claude-flagship"}


@pytest.fixture
def finance(make_client, user_token):
    return lambda **kw: make_client(user_token("u-finance"), **kw)


async def ask(app_client, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    async with app_client(APP) as client:
        response = await client.post("/v1/ai/chat", json=payload)
    return response.status_code, response.json()


async def approve(finance, approval_id: str) -> None:
    async with finance() as client:
        response = await client.post(f"/v1/admin/approvals/{approval_id}/approve", json={"note": "approved"})
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "approved"


async def approved_exception(app_client, finance, services) -> tuple[str, dict[str, Any], str, int]:
    """(workflow, budget, approval ID, approved amount) for an approved budget approval."""
    wf = workflow()
    budget = await set_budget(finance, wf, 0, on_exhaustion="approval_required")
    _, first = await ask(app_client, metered(wf))
    assert first["status"] == "approval_required" and first["error"]["code"] == "QUOTA_EXCEEDED", first
    approval_id = first["approval"]["approval_id"]
    await approve(finance, approval_id)
    async with services.sessionmaker() as session:
        row = await session.get(ApprovalRequestRow, approval_id)
    assert row is not None and row.kind == "budget" and row.rule_ids == [budget["id"]]
    assert row.approved_amount_micros is not None and row.approved_amount_micros > 0
    return wf, budget, approval_id, row.approved_amount_micros


async def claim_for(services, approval_id: str, **values: Any) -> str:
    """Mark the approval claimed by a new run, as Govern does before the run starts; returns the run ID."""
    run_id = f"run_{ulid()}"
    async with services.sessionmaker() as session, session.begin():
        await session.execute(
            update(ApprovalRequestRow)
            .where(ApprovalRequestRow.id == approval_id)
            .values(status="consumed", consumed_by_run_id=run_id, consumed_at=datetime.now(UTC), **values)
        )
    return run_id


def chain(wf: str) -> tuple[tuple[str, str], ...]:
    return scope_chain(
        tenant_id="gct",
        department_id="customer-care",
        application_id=APP,
        principal_pseudonym=None,
        workflow_id=wf,
    )


def request(wf: str, run_id: str, amount: int, approval_id: str | None) -> ReservationRequest:
    return ReservationRequest(
        run_id=run_id,
        tenant_id="gct",
        scope_chain=chain(wf),
        amount_micros=amount,
        idempotency_key=ulid(),
        domain=ILLUSTRATIVE,
        approval_id=approval_id,
    )


async def held(services, bucket: Bucket) -> tuple[int, int]:
    state = (await services.quota.bucket_states("gct", [bucket]))[bucket.key]
    return state.spent, state.reserved


async def funded_by(services, approval_id: str) -> list[BudgetReservationRow]:
    async with services.sessionmaker() as session:
        rows = await session.scalars(
            select(BudgetReservationRow).where(BudgetReservationRow.approval_id == approval_id)
        )
        return list(rows)


async def test_approved_exception_funds_exactly_one_request(app_client, finance, services, db) -> None:
    wf, budget, approval_id, approved = await approved_exception(app_client, finance, services)

    _, resumed = await ask(app_client, {**metered(wf), "approval_id": approval_id})
    assert resumed["status"] == "completed", resumed.get("error")
    assert resumed["budget"]["reserved_micros"] <= approved

    [reservation] = await funded_by(services, approval_id)
    assert reservation.run_id == resumed["run_id"] and reservation.estimate_micros <= approved
    spent, reserved = await held(services, wf_bucket(wf))
    assert reserved == 0 and 0 < spent <= approved, "past the zero cap by at most the approved amount"

    audit = await db.scalar(
        select(AuditLogRow).where(
            AuditLogRow.action == "budget.approved_exception",
            AuditLogRow.target_id == reservation.reservation_id,
        )
    )
    assert audit is not None and audit.actor_kind == "system"
    assert audit.details["approval_id"] == approval_id and audit.details["budget_ids"] == [budget["id"]]
    assert audit.details["approved_amount_micros"] == approved
    alert = await db.scalar(
        select(BudgetAlertRow).where(
            BudgetAlertRow.kind == "approved_exception",
            BudgetAlertRow.reservation_id == reservation.reservation_id,
        )
    )
    assert alert is not None and alert.budget_id == budget["id"] and alert.provenance == "estimated"
    async with finance() as client:
        listed = (await client.get("/v1/admin/budgets/alerts")).json()["alerts"]
    assert any(a["kind"] == "approved_exception" and a["budget_id"] == budget["id"] for a in listed)

    # A replay through the API is refused by Govern (the approval is consumed) ...
    status_code, replay = await ask(app_client, {**metered(wf), "approval_id": approval_id})
    assert status_code == 409 and replay["error"]["code"] == "APPROVAL_NOT_USABLE"
    # ... and FinOps refuses a second use itself, even by the run that claimed it, under a new key.
    with pytest.raises(QuotaDeniedError) as denied:
        await services.quota.reserve(request(wf, resumed["run_id"], approved, approval_id))
    assert denied.value.rule_id == budget["id"] and denied.value.on_exhaustion == "approval_required"
    assert ALREADY_USED in str(denied.value)
    assert await held(services, wf_bucket(wf)) == (spent, 0)
    assert len(await funded_by(services, approval_id)) == 1


async def test_expired_approval_is_refused(app_client, finance, services) -> None:
    wf, budget, approval_id, approved = await approved_exception(app_client, finance, services)
    # Claimed in time, but expired before the reservation (FinOps checks expiry itself).
    run_id = await claim_for(services, approval_id, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(QuotaDeniedError) as denied:
        await services.quota.reserve(request(wf, run_id, approved, approval_id))
    assert denied.value.rule_id == budget["id"] and "it has expired" in str(denied.value)
    assert await held(services, wf_bucket(wf)) == (0, 0)
    assert await funded_by(services, approval_id) == []


async def test_approval_not_claimed_by_this_run_is_refused(app_client, finance, services) -> None:
    wf, _, approval_id, approved = await approved_exception(app_client, finance, services)
    # Approved but never claimed: a caller cannot apply it by passing its ID.
    with pytest.raises(QuotaDeniedError) as unclaimed:
        await services.quota.reserve(request(wf, f"run_{ulid()}", approved, approval_id))
    assert "it was not claimed by this run" in str(unclaimed.value)
    await claim_for(services, approval_id)
    with pytest.raises(QuotaDeniedError) as other_run:
        await services.quota.reserve(request(wf, f"run_{ulid()}", approved, approval_id))
    assert "it was not claimed by this run" in str(other_run.value)
    assert await funded_by(services, approval_id) == []


async def test_larger_envelope_on_resume_needs_a_new_approval(app_client, finance, services, db) -> None:
    wf, budget, approval_id, approved = await approved_exception(app_client, finance, services)
    # Prices rose after approval: the identical request's worst case now exceeds what was approved.
    async with services.sessionmaker() as session, session.begin():
        await session.execute(
            update(ApprovalRequestRow)
            .where(ApprovalRequestRow.id == approval_id)
            .values(approved_amount_micros=approved - 1)
        )

    _, resumed = await ask(app_client, {**metered(wf), "approval_id": approval_id})
    assert resumed["status"] == "approval_required" and resumed["error"]["code"] == "QUOTA_EXCEEDED"
    assert "needs more than the approved amount" in resumed["error"]["message"]
    assert resumed["error"]["rule_ids"] == [budget["id"]]
    renewed = resumed["approval"]["approval_id"]
    assert renewed != approval_id
    row = await db.get(ApprovalRequestRow, renewed)
    assert row is not None and row.kind == "budget" and row.approved_amount_micros == approved
    assert await funded_by(services, approval_id) == []
    attempts = await db.scalar(
        select(func.count()).select_from(ModelAttemptRow).where(ModelAttemptRow.run_id == resumed["run_id"])
    )
    assert attempts == 0, "nothing billable ran"


async def test_concurrent_uses_fund_exactly_one_reservation(app_client, finance, services) -> None:
    wf, _, approval_id, approved = await approved_exception(app_client, finance, services)
    run_id = await claim_for(services, approval_id)

    async def use() -> Reservation | QuotaDeniedError:
        try:
            return await services.quota.reserve(request(wf, run_id, approved, approval_id))
        except QuotaDeniedError as exc:
            return exc

    results = await asyncio.gather(*(use() for _ in range(6)))
    funded = [r for r in results if isinstance(r, Reservation)]
    refused = [r for r in results if isinstance(r, QuotaDeniedError)]
    assert len(funded) == 1 and len(refused) == 5
    assert all(ALREADY_USED in str(exc) for exc in refused)
    assert await held(services, wf_bucket(wf)) == (0, approved)
    [row] = await funded_by(services, approval_id)
    assert row.reservation_id == funded[0].reservation_id
    await services.quota.settle(funded[0], 0)
    assert await held(services, wf_bucket(wf)) == (0, 0)


async def test_concurrent_resumes_complete_exactly_once(app_client, finance, services) -> None:
    wf, _, approval_id, _ = await approved_exception(app_client, finance, services)
    payload = {**metered(wf), "approval_id": approval_id}
    results = await asyncio.gather(*(ask(app_client, payload) for _ in range(4)))
    completed = [body for _, body in results if body.get("status") == "completed"]
    assert len(completed) == 1, [body.get("error") for _, body in results]
    assert all(code == 409 for code, body in results if body.get("status") != "completed")
    assert [row.run_id for row in await funded_by(services, approval_id)] == [completed[0]["run_id"]]


class Unverified(RedisQuotaService):
    """Applies the approval without the durable check, as a verification racing a first use would."""

    def __init__(self, base: RedisQuotaService, exception: ApprovedException) -> None:
        super().__init__(base.redis, base.sessionmaker, base.prices)
        self.exception = exception

    async def _approval(self, request: ReservationRequest, now: datetime) -> _Attempt:
        return _Attempt(exception=self.exception)


async def test_unique_index_backstops_a_lost_marker(app_client, finance, services) -> None:
    wf, budget, approval_id, approved = await approved_exception(app_client, finance, services)
    _, resumed = await ask(app_client, {**metered(wf), "approval_id": approval_id})
    assert resumed["status"] == "completed", resumed.get("error")
    before = await held(services, wf_bucket(wf))
    await services.redis.delete(QuotaKeys("gct").approval(approval_id))  # Redis lost the marker

    quota = Unverified(
        services.quota, ApprovedException(approval_id, frozenset({budget["id"]}), 10 * approved)
    )
    with pytest.raises(ConflictError) as conflict:
        await quota.reserve(request(wf, resumed["run_id"], approved, approval_id))
    assert conflict.value.code == "BUDGET_APPROVAL_USED"
    assert await held(services, wf_bucket(wf)) == before, "the second hold was undone"
    assert len(await funded_by(services, approval_id)) == 1


@pytest.mark.parametrize(
    ("headroom", "offered"), [(1, True), (0, True), (-1, False)], ids=["under-cap", "at-cap", "over-cap"]
)
async def test_an_approval_is_offered_only_when_it_could_admit_the_request(
    app_client, finance, services, db, headroom: int, offered: bool
) -> None:
    """An approval covers the request's own share only (ADR-0014 amendment). While the budget is at or under
    its cap one can admit the request, so it is offered; past its cap none could, so none is created and the
    denial says the budget is already exhausted."""
    wf = workflow()
    _, first = await ask(app_client, metered(wf))  # spends before the budget exists
    assert first["status"] == "completed", first.get("error")
    spent, reserved = await held(services, wf_bucket(wf))
    assert spent > 0 and reserved == 0
    budget = await set_budget(finance, wf, spent + headroom, on_exhaustion="approval_required")

    _, denied = await ask(app_client, metered(wf))
    assert denied["error"]["code"] == "QUOTA_EXCEEDED" and denied["error"]["rule_ids"] == [budget["id"]]
    if offered:
        assert denied["status"] == "approval_required" and "reason" not in denied["error"]["details"]
        approval_id = denied["approval"]["approval_id"]
        await approve(finance, approval_id)
        _, resumed = await ask(app_client, {**metered(wf), "approval_id": approval_id})
        assert resumed["status"] == "completed", resumed.get("error")  # the approval offered is usable
        return
    assert denied["status"] == "blocked" and denied["outcome"] == "quota_denied"
    assert denied["approval"]["approval_id"] is None
    assert denied["error"]["details"] == {"reason": "budget_already_exhausted"}
    assert denied["error"]["message"].startswith("Budget already exhausted; raise the budget.")
    created = await db.scalars(
        select(ApprovalRequestRow).where(ApprovalRequestRow.run_id == denied["run_id"])
    )
    assert list(created) == []


async def test_approval_never_changes_the_sovereign_path(tester_client, finance, services, db) -> None:
    """The exception lifts a cap only: confidential traffic still takes the sovereign direct path."""
    # The pool is shared: every simulated sovereign run in the suite adds its wall-clock GPU estimate to
    # today's bucket. Capped at what it already holds, the pool is exhausted by this request alone.
    today = period_key("daily", datetime.now(UTC))
    pool = Bucket("gpu_pool", "gct-qwen-finetuned", "daily", today, ILLUSTRATIVE)
    state = (await services.quota.bucket_states("gct", [pool])).get(pool.key)
    async with finance() as client:
        response = await client.post(
            "/v1/admin/budgets",
            json={
                "scope_type": "gpu_pool",
                "scope_id": "gct-qwen-finetuned",
                "period": "daily",
                "cap_micros": state.spent + state.reserved if state else 0,
                "on_exhaustion": "approval_required",
            },
        )
    assert response.status_code == 201, response.text
    budget = response.json()
    payload = chat_body(
        "Summarise FR-9",
        data_classification="confidential",
        baseline_comparison=True,
        routing={"mode": "hybrid", "preferred_model": "claude-flagship"},
        **console("gct-fraud-investigator"),
    )
    try:
        async with tester_client() as client:
            first = (await client.post("/v1/ai/chat", json=payload)).json()
        assert first["status"] == "approval_required" and first["error"]["rule_ids"] == [budget["id"]]
        approval_id = first["approval"]["approval_id"]
        row = await db.get(ApprovalRequestRow, approval_id)
        # It covers the GPU estimate the pool needed; the request's API spend on this path is $0.
        assert row is not None and (row.approved_amount_micros or 0) > 0
        await approve(finance, approval_id)
        async with tester_client() as client:
            resumed = (await client.post("/v1/ai/chat", json={**payload, "approval_id": approval_id})).json()
        assert resumed["status"] == "completed", resumed.get("error")
        assert resumed["execution_path"] == "sovereign-direct"
        assert resumed["selected_model"] == "gct-qwen-finetuned"
        models = list(
            await db.scalars(
                select(ModelAttemptRow.model_id).where(ModelAttemptRow.run_id == resumed["run_id"])
            )
        )
        assert models and not set(models) & EXTERNAL
        [reservation] = await funded_by(services, approval_id)
        alert = await db.scalar(
            select(BudgetAlertRow).where(
                BudgetAlertRow.kind == "approved_exception",
                BudgetAlertRow.reservation_id == reservation.reservation_id,
            )
        )
        assert alert is not None and alert.scope_type == "gpu_pool"
    finally:
        async with services.sessionmaker() as session, session.begin():
            await session.execute(delete(BudgetRow).where(BudgetRow.id == budget["id"]))
        await services.quota.publish_budgets("gct")
