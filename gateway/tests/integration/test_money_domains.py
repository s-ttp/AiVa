"""Money domains (ADR-0008 amendment). Live budgets count only runs that can spend money. Replayed and
simulated runs are checked against the same caps in an illustrative domain, and a public visitor's runs in a
domain of their own, so a public replay demo can neither drain the live budgets nor block other visitors."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from aiva_gateway.catalog.models import ModelSpec
from aiva_gateway.core.enums import Provenance
from aiva_gateway.core.ids import ulid
from aiva_gateway.db.models import BudgetAlertRow, UsageLedgerRow
from aiva_gateway.finops.scopes import ILLUSTRATIVE, LIVE, Bucket, period_key, scope_chain, visitor_domain
from aiva_gateway.pipeline.interfaces import (
    Charge,
    InferenceAdapter,
    InferenceRequest,
    InferenceResult,
    QuotaDeniedError,
    ReservationRequest,
)
from tests.conftest import with_overrides
from tests.integration.test_budgets import APP, budget_status, metered, set_budget, workflow


@pytest.fixture
def finance(make_client, user_token):
    return lambda **kw: make_client(user_token("u-finance"), **kw)


class _Labelled:
    def __init__(self, inner: InferenceAdapter, label: Provenance) -> None:
        self.inner, self.label = inner, label

    async def complete(self, request: InferenceRequest) -> InferenceResult:
        result = await self.inner.complete(request)
        return replace(result, usage=replace(result.usage, provenance=self.label), provenance=self.label)


class Labelled:
    """The simulated adapters, labelled as a live provider (`measured`) or a recording (`replayed`)."""

    def __init__(self, inner: Any, label: Provenance) -> None:
        self.inner, self.label = inner, label

    def for_model(self, model: ModelSpec) -> InferenceAdapter:
        return _Labelled(self.inner.for_model(model), self.label)

    async def aclose(self) -> None:
        return None


def in_mode(services: Any, inference_mode: str, label: Provenance) -> Any:
    settings = services.settings.model_copy(update={"inference_mode": inference_mode})
    return with_overrides(services, settings=settings, adapters=Labelled(services.adapters, label))


def workflow_bucket(wf: str, domain: str) -> Bucket:
    return Bucket("workflow", f"{APP}/{wf}", "workflow", "lifetime", domain)


def daily(scope_type: str, scope_id: str, domain: str) -> Bucket:
    return Bucket(scope_type, scope_id, "daily", period_key("daily", datetime.now(UTC)), domain)


async def spent(services: Any, bucket: Bucket) -> int:
    state = (await services.quota.bucket_states("gct", [bucket])).get(bucket.key)
    return state.spent if state else 0


async def test_a_hundred_replay_runs_never_move_a_live_bucket(app_client, services, db) -> None:
    replay = in_mode(services, "replay", Provenance.REPLAYED)
    wf = workflow()
    live = [daily("tenant", "gct", LIVE), daily("application", APP, LIVE), workflow_bucket(wf, LIVE)]
    before = [await spent(services, b) for b in live]
    run_ids, committed = [], 0
    async with app_client(APP, svc=replay) as client:
        for _ in range(100):
            body = (await client.post("/v1/ai/chat", json=metered(wf))).json()
            assert body["status"] == "completed", body.get("error")
            run_ids.append(body["run_id"])
            committed += body["budget"]["committed_micros"]
    assert committed > 0
    assert [await spent(services, b) for b in live] == before
    # The runs were checked and counted in the illustrative domain, labelled as what they are.
    assert await spent(services, workflow_bucket(wf, ILLUSTRATIVE)) == committed
    labels = await db.scalars(
        select(UsageLedgerRow.measured_or_estimated)
        .distinct()
        .where(UsageLedgerRow.run_id.in_(run_ids), UsageLedgerRow.charge_type != "gpu_estimate")
    )
    assert set(labels) == {"replayed"}, "ledger rows keep their recorded amounts and provenance"


@pytest.mark.parametrize("spend_in", [LIVE, ILLUSTRATIVE])
async def test_each_domain_is_capped_without_touching_the_other(
    app_client, services, finance, spend_in: str
) -> None:
    modes = {LIVE: in_mode(services, "live", Provenance.MEASURED), ILLUSTRATIVE: services}
    other = ILLUSTRATIVE if spend_in == LIVE else LIVE
    wf = workflow()

    async def step(domain: str) -> dict[str, Any]:
        async with app_client(APP, svc=modes[domain]) as client:
            return (await client.post("/v1/ai/chat", json=metered(wf))).json()

    first = await step(spend_in)
    assert first["status"] == "completed", first.get("error")
    # Cap the workflow at one envelope: the spend already committed leaves no room for another run in that
    # domain, while the other domain still has the whole cap.
    budget = await set_budget(finance, wf, first["budget"]["reserved_micros"])
    denied = await step(spend_in)
    assert denied["status"] == "blocked" and denied["error"]["code"] == "QUOTA_EXCEEDED"
    admitted = await step(other)
    assert admitted["status"] == "completed", admitted.get("error")

    status = await budget_status(finance, budget["id"])  # this gateway's configured mode is simulated
    illustrative = status if status["domain"] == "illustrative" else status["other_domain"]
    live = status["other_domain"] if status["domain"] == "illustrative" else status
    assert status["domain"] == "illustrative"
    assert live["spent_micros"] > 0 and live["provenance"] == "measured"
    assert illustrative["spent_micros"] > 0 and illustrative["provenance"] == "simulated"


async def test_each_visitor_has_illustrative_budgets_of_their_own(services, finance, db) -> None:
    wf = workflow()
    await set_budget(finance, wf, 1_000)
    chain = scope_chain(
        tenant_id="gct", department_id=None, application_id=APP, principal_pseudonym=None, workflow_id=wf
    )

    def request(domain: str, amount: int) -> ReservationRequest:
        return ReservationRequest(
            run_id=ulid(),
            tenant_id="gct",
            scope_chain=chain,
            amount_micros=amount,
            idempotency_key=ulid(),
            domain=domain,
        )

    visitor, neighbour = visitor_domain("usr_visitor00a"), visitor_domain("usr_visitor00b")
    reservation = await services.quota.reserve(request(visitor, 1_000))
    charge = Charge("claude-flagship", 1_000, provenance=Provenance.REPLAYED)
    await services.quota.settle(reservation, 1_000, charges=(charge,))
    with pytest.raises(QuotaDeniedError):
        await services.quota.reserve(request(visitor, 1))
    for domain in (neighbour, ILLUSTRATIVE, LIVE):  # the visitor's spend is theirs alone
        await services.quota.release(await services.quota.reserve(request(domain, 1_000)))

    # The visitor's threshold and hard-cap alerts carry their domain and stay out of the admin list.
    rows = list(await db.scalars(select(BudgetAlertRow).where(BudgetAlertRow.scope_id == f"{APP}/{wf}")))
    assert rows and {row.domain for row in rows} == {visitor}
    async with finance() as client:
        listed = (await client.get("/v1/admin/budgets/alerts", params={"limit": 500})).json()["alerts"]
    assert not any(alert["scope_id"] == f"{APP}/{wf}" for alert in listed)


@pytest.mark.parametrize(
    "inference_mode, label",
    [("simulated", Provenance.SIMULATED), ("replay", Provenance.REPLAYED), ("live", Provenance.MEASURED)],
)
async def test_a_reservation_is_labelled_by_its_domain(app_client, services, inference_mode, label) -> None:
    # The hold is exact only in the live store; an illustrative reservation is never shown as measured.
    svc = services if inference_mode == "simulated" else in_mode(services, inference_mode, label)
    async with app_client(APP, svc=svc) as client:
        body = (await client.post("/v1/ai/chat", json=metered(workflow()))).json()
    assert body["status"] == "completed", body.get("error")
    assert body["budget"]["reserved_micros"] > 0 and body["budget"]["provenance"] == label.value
