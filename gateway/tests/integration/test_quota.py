"""RedisQuotaService against real Redis and Postgres: atomic hierarchical caps, idempotency, fail-closed.

These tests use their own tenant so they never touch GCT's live counters.
"""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from aiva_gateway.core.enums import Provenance
from aiva_gateway.core.errors import AivaError, DependencyUnavailableError
from aiva_gateway.core.ids import new_id, ulid
from aiva_gateway.db.models import BudgetAlertRow, BudgetReservationRow, BudgetRow, TenantRow
from aiva_gateway.finops import quota as quota_module
from aiva_gateway.finops.budgets import budget_id
from aiva_gateway.finops.ledger import LedgerReservation
from aiva_gateway.finops.quota import BucketState, QuotaKeys, RedisQuotaService
from aiva_gateway.finops.scopes import ILLUSTRATIVE, Bucket, period_key, scope_chain
from aiva_gateway.pipeline.interfaces import Charge, QuotaDeniedError, ReservationLine, ReservationRequest

TENANT = "finops-test"


@pytest.fixture(scope="module", autouse=True)
async def tenant(services) -> str:
    async with services.sessionmaker() as session, session.begin():
        await session.execute(
            insert(TenantRow)
            .values(id=TENANT, name="FinOps test tenant", currency="USD")
            .on_conflict_do_nothing()
        )
    return TENANT


def unique(prefix: str) -> str:
    return f"{prefix}-{ulid()[-10:].lower()}"


async def set_budget(
    services,
    scope_type: str,
    scope_id: str,
    period: str,
    cap: int,
    *,
    thresholds: Sequence[float] = (0.8, 0.95),
    on_exhaustion: str = "block",
) -> str:
    identifier = budget_id(scope_type, scope_id, period)
    values = {
        "id": identifier,
        "tenant_id": TENANT,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "period": period,
        "cap_micros": cap,
        "alert_thresholds": list(thresholds),
        "on_exhaustion": on_exhaustion,
    }
    async with services.sessionmaker() as session, session.begin():
        statement = insert(BudgetRow).values(**values)
        await session.execute(
            statement.on_conflict_do_update(
                constraint="uq_budget_tenant_id",
                set_={k: statement.excluded[k] for k in ("cap_micros", "alert_thresholds", "on_exhaustion")},
            )
        )
    await services.quota.publish_budgets(TENANT)
    return identifier


def request(
    app: str,
    amount: int,
    *,
    key: str | None = None,
    lines: tuple[ReservationLine, ...] = (),
    user: str | None = None,
    workflow: str | None = None,
) -> ReservationRequest:
    chain = scope_chain(
        tenant_id=TENANT,
        department_id=None,
        application_id=app,
        principal_pseudonym=user,
        workflow_id=workflow,
    )
    return ReservationRequest(
        run_id=new_id("run"),
        tenant_id=TENANT,
        scope_chain=chain,
        amount_micros=amount,
        idempotency_key=key or new_id("idem"),
        domain=ILLUSTRATIVE,
        lines=lines,
    )


def monthly(scope_type: str, scope_id: str) -> Bucket:
    return Bucket(scope_type, scope_id, "monthly", period_key("monthly", datetime.now(UTC)))


async def state(services, bucket: Bucket) -> BucketState:
    return (await services.quota.bucket_states(TENANT, [bucket]))[bucket.key]


async def try_reserve(services, req: ReservationRequest) -> LedgerReservation | None:
    try:
        reservation = await services.quota.reserve(req)
    except QuotaDeniedError:
        return None
    assert isinstance(reservation, LedgerReservation)
    return reservation


async def test_reserve_settle_and_release_move_counters(services) -> None:
    app = unique("app")
    await set_budget(services, "application", app, "monthly", 10_000)
    reservation = await services.quota.reserve(request(app, 1_000))
    bucket = monthly("application", app)
    assert (await state(services, bucket)).reserved == 1_000
    async with services.sessionmaker() as session:
        journal = await session.get(BudgetReservationRow, reservation.reservation_id)
    assert journal is not None and journal.status == "reserved", "journaled before any billable call"
    released = await services.quota.settle(reservation, 400)
    after = await state(services, bucket)
    assert (released, after.spent, after.reserved) == (600, 400, 0)

    second = await services.quota.reserve(request(app, 2_000))
    assert isinstance(second, LedgerReservation)
    await services.quota.release(second)
    assert (await state(services, bucket)).reserved == 0


async def test_concurrent_reservations_never_exceed_the_cap(services) -> None:
    app = unique("app")
    await set_budget(services, "application", app, "monthly", 10_000)
    results = await asyncio.gather(*(try_reserve(services, request(app, 1_300)) for _ in range(50)))
    granted = [r for r in results if r is not None]
    assert len(granted) == 7  # 7 x 1,300 = 9,100 <= 10,000 < 8 x 1,300
    held = await state(services, monthly("application", app))
    assert held.reserved == 9_100 and held.spent == 0


async def test_reserve_settle_storm_keeps_spend_within_the_cap(services) -> None:
    app, workflow = unique("app"), unique("wf")
    cap = 20_000
    await set_budget(services, "workflow", f"{app}/{workflow}", "workflow", cap)
    settled: list[int] = []

    async def worker(index: int) -> None:
        for _ in range(20):
            reservation = await try_reserve(services, request(app, 900, workflow=workflow))
            if reservation is None:
                return
            await asyncio.sleep(0)
            actual = 500 + (index % 4) * 100  # never above the reservation
            await services.quota.settle(reservation, actual)
            settled.append(actual)

    await asyncio.gather(*(worker(i) for i in range(30)))
    bucket = Bucket("workflow", f"{app}/{workflow}", "workflow", "lifetime")
    final = await state(services, bucket)
    assert final.reserved == 0
    assert final.spent == sum(settled) <= cap
    assert cap - final.spent < 900, "the workflow budget was used up to its last full envelope"


async def test_reservations_are_idempotent_by_key(services) -> None:
    app = unique("app")
    await set_budget(services, "application", app, "monthly", 10_000)
    key = new_id("idem")
    same = await asyncio.gather(*(services.quota.reserve(request(app, 2_500, key=key)) for _ in range(10)))
    assert len({r.reservation_id for r in same}) == 1
    bucket = monthly("application", app)
    assert (await state(services, bucket)).reserved == 2_500
    await services.quota.settle(same[0], 1_000)
    await services.quota.settle(same[0], 1_000)  # a retried settle is a no-op
    assert (await state(services, bucket)).spent == 1_000
    with pytest.raises(AivaError) as exc:
        await services.quota.reserve(request(app, 2_500, key=key))
    assert exc.value.code == "RESERVATION_ALREADY_SETTLED"


async def test_strictest_exhaustion_action_wins(services) -> None:
    app, workflow = unique("app"), unique("wf")
    app_budget = await set_budget(
        services, "application", app, "monthly", 100, on_exhaustion="lower_cost_route"
    )
    with pytest.raises(QuotaDeniedError) as only_app:
        await services.quota.reserve(request(app, 500, workflow=workflow))
    assert (only_app.value.on_exhaustion, only_app.value.rule_id) == ("lower_cost_route", app_budget)
    assert only_app.value.scope_type == "application"
    workflow_budget = await set_budget(services, "workflow", f"{app}/{workflow}", "workflow", 100)
    with pytest.raises(QuotaDeniedError) as both:
        await services.quota.reserve(request(app, 500, workflow=workflow))
    assert (both.value.on_exhaustion, both.value.rule_id) == ("block", workflow_budget)


async def test_request_cap_bounds_a_single_envelope(services) -> None:
    app = unique("app")
    rule = await set_budget(services, "request", app, "request", 1_000)
    with pytest.raises(QuotaDeniedError) as exc:
        await services.quota.reserve(request(app, 1_500))
    assert (exc.value.rule_id, exc.value.scope_type) == (rule, "request")
    assert await try_reserve(services, request(app, 900)) is not None


async def test_model_and_gpu_pool_scopes_are_capped(services) -> None:
    app, model, pool = unique("app"), unique("model"), unique("pool")
    await set_budget(services, "model", model, "daily", 1_000)
    await set_budget(services, "gpu_pool", pool, "monthly", 100)
    line = (ReservationLine(model, 600),)
    assert await try_reserve(services, request(app, 600, lines=line)) is not None
    with pytest.raises(QuotaDeniedError) as by_model:
        await services.quota.reserve(request(app, 600, lines=line))
    assert (by_model.value.scope_type, by_model.value.scope_id) == ("model", model)
    local = (ReservationLine(unique("local"), 0, gpu_pool=pool, gpu_micros=80),)
    assert await try_reserve(services, request(app, 0, lines=local)) is not None
    with pytest.raises(QuotaDeniedError) as by_pool:
        await services.quota.reserve(request(app, 0, lines=local))
    assert (by_pool.value.scope_type, by_pool.value.scope_id) == ("gpu_pool", pool)


async def test_template_budget_caps_each_scope_separately(services) -> None:
    app = unique("app")
    await set_budget(services, "user", "default", "daily", 1_000)
    try:
        assert await try_reserve(services, request(app, 800, user="usr_a" + app)) is not None
        assert await try_reserve(services, request(app, 800, user="usr_b" + app)) is not None
        assert await try_reserve(services, request(app, 800, user="usr_a" + app)) is None
    finally:
        await set_budget(services, "user", "default", "daily", 10**12)


async def test_budget_changes_reach_every_process(services) -> None:
    app = unique("app")
    await set_budget(services, "application", app, "monthly", 10_000)
    other = RedisQuotaService(services.redis, services.sessionmaker, services.prices)
    assert await other.reserve(request(app, 600))  # caches the budget book
    await set_budget(services, "application", app, "monthly", 500)  # published by another process
    with pytest.raises(QuotaDeniedError):
        await other.reserve(request(app, 100))


async def test_open_holds_rebuild_from_the_journal(services) -> None:
    app = unique("app")
    await set_budget(services, "application", app, "monthly", 10_000)
    reservation = await services.quota.reserve(request(app, 700))
    bucket = monthly("application", app)
    await services.redis.delete(QuotaKeys(TENANT).bucket(bucket.key))  # Redis lost its data
    assert (await state(services, bucket)).reserved == 700
    await services.quota.settle(reservation, 300)
    after = await state(services, bucket)
    assert (after.spent, after.reserved) == (300, 0)


async def test_journal_failure_undoes_the_hold(services, monkeypatch) -> None:
    app = unique("app")
    await set_budget(services, "application", app, "monthly", 10_000)

    def broken_row(reservation, *, pricing_version):
        row = BudgetReservationRow(reservation_id=reservation.reservation_id, run_id=reservation.run_id)
        return row  # missing NOT NULL columns: the insert fails

    monkeypatch.setattr(quota_module, "journal_row", broken_row)
    with pytest.raises(DependencyUnavailableError) as exc:
        await services.quota.reserve(request(app, 700))
    assert exc.value.code == "QUOTA_STORE_UNAVAILABLE"
    monkeypatch.undo()
    assert (await state(services, monthly("application", app))).reserved == 0


async def test_unreachable_quota_store_fails_closed(services) -> None:
    dead = Redis.from_url("redis://127.0.0.1:1/0", decode_responses=True)
    broken = RedisQuotaService(dead, services.sessionmaker, services.prices)
    with pytest.raises(DependencyUnavailableError) as exc:
        await broken.reserve(request(unique("app"), 100))
    assert exc.value.code == "QUOTA_STORE_UNAVAILABLE"
    await dead.aclose()


async def _alerts(services, scope_id: str) -> list[BudgetAlertRow]:
    async with services.sessionmaker() as session:
        rows = await session.scalars(
            select(BudgetAlertRow)
            .where(BudgetAlertRow.tenant_id == TENANT, BudgetAlertRow.scope_id == scope_id)
            .order_by(BudgetAlertRow.created_at)
        )
        return list(rows)


async def test_threshold_and_hard_cap_alerts_fire_once(services) -> None:
    app = unique("app")
    await set_budget(services, "application", app, "monthly", 10_000)

    async def spend(amount: int) -> None:
        reservation = await services.quota.reserve(request(app, amount))
        await services.quota.settle(
            reservation, amount, charges=(Charge("m", amount, provenance=Provenance.MEASURED),)
        )

    await spend(7_900)
    assert await _alerts(services, app) == []
    await spend(200)
    await spend(1_500)
    await spend(400)
    assert await try_reserve(services, request(app, 1)) is None
    alerts = await _alerts(services, app)
    assert [(a.kind, a.threshold_bps) for a in alerts] == [
        ("threshold", 8000),
        ("threshold", 9500),
        ("hard_cap", 10_000),
    ]
    assert alerts[0].spent_micros == 8_100 and alerts[0].cap_micros == 10_000
    assert {a.provenance for a in alerts} == {"measured"}


async def test_denial_raises_a_hard_cap_alert(services) -> None:
    app = unique("app")
    rule = await set_budget(services, "application", app, "monthly", 1_000)
    req = request(app, 2_000)
    assert await try_reserve(services, req) is None
    [alert] = await _alerts(services, app)
    assert (alert.kind, alert.budget_id, alert.run_id, alert.amount_micros) == (
        "hard_cap",
        rule,
        req.run_id,
        2_000,
    )


async def test_overrun_is_committed_and_alerted(services) -> None:
    app = unique("app")
    await set_budget(services, "application", app, "monthly", 10_000)
    reservation = await services.quota.reserve(request(app, 100))
    await services.quota.settle(reservation, 150)
    assert (await state(services, monthly("application", app))).spent == 150
    async with services.sessionmaker() as session:
        overrun = await session.scalar(
            select(BudgetAlertRow).where(BudgetAlertRow.reservation_id == reservation.reservation_id)
        )
    assert overrun is not None and overrun.kind == "reservation_overrun" and overrun.amount_micros == 50
