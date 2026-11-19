"""Billed tool calls against the real quota store and ledger: a planned web search is held by the reservation
before it runs, and its `tool_call` charge settles in the shared scopes only (never a model budget), in the
live domain only when it is money, and back through reconciliation with its label."""

from typing import Any

from aiva_gateway.core.enums import ChargeType, Provenance
from aiva_gateway.core.ids import ledger_id, ulid
from aiva_gateway.db.models import UsageLedgerRow
from aiva_gateway.finops.envelope import build_envelope
from aiva_gateway.finops.reconcile import ledger_spend
from aiva_gateway.finops.scopes import LIVE, scope_chain
from aiva_gateway.pipeline.interfaces import Charge, ReservationRequest
from tests.integration.test_budgets import APP, metered, workflow
from tests.integration.test_money_domains import daily, spent, workflow_bucket

SEARCH = "web.search"
GEMINI = "gemini-flash-lite"


async def reserve_with_searches(services: Any, wf: str, searches: int) -> Any:
    gemini = services.catalog.model(GEMINI)
    envelope = build_envelope(
        services.prices,
        upstream=[gemini],
        upstream_attempts=1,
        input_tokens=200,
        max_output_tokens=50,
        tools={SEARCH: searches},
    )
    chain = scope_chain(
        tenant_id="gct", department_id=None, application_id=APP, principal_pseudonym=None, workflow_id=wf
    )
    reservation = await services.quota.reserve(
        ReservationRequest(
            run_id=ulid(),
            tenant_id="gct",
            scope_chain=chain,
            amount_micros=envelope.amount_micros,
            idempotency_key=ulid(),
            domain=LIVE,
            lines=envelope.lines,
        )
    )
    return envelope, reservation


async def test_a_search_is_held_then_settled_in_the_shared_scopes_only(services) -> None:  # type: ignore[no-untyped-def]
    wf = workflow()
    model_bucket = daily("model", GEMINI, LIVE)
    model_before = await spent(services, model_bucket)
    envelope, reservation = await reserve_with_searches(services, wf, searches=2)
    assert envelope.tool_micros == 16_000 and reservation.amount_micros == envelope.amount_micros
    assert {line.model_id for line in reservation.lines} == {GEMINI}  # the search is no model line

    charges = (
        Charge(GEMINI, 500, provenance=Provenance.MEASURED),
        Charge(SEARCH, 8_000, provenance=Provenance.MEASURED, kind="tool"),  # one search was made
    )
    await services.quota.settle(reservation, 8_500, charges=charges)
    assert (
        await spent(services, workflow_bucket(wf, LIVE)) == 8_500
    )  # the workflow budget pays for the search
    assert await spent(services, model_bucket) - model_before == 500  # the model budget does not
    tool_bucket = daily("model", SEARCH, LIVE)
    assert await spent(services, tool_bucket) == 0, "a tool never gets a model bucket"


async def test_a_simulated_search_never_counts_in_a_live_budget(services) -> None:  # type: ignore[no-untyped-def]
    wf = workflow()
    _, reservation = await reserve_with_searches(services, wf, searches=1)
    charges = (
        Charge(GEMINI, 500, provenance=Provenance.MEASURED),
        Charge(SEARCH, 8_000, provenance=Provenance.SIMULATED, kind="tool"),
    )
    await services.quota.settle(reservation, 8_500, charges=charges)
    assert await spent(services, workflow_bucket(wf, LIVE)) == 500


async def test_reconciliation_reads_a_search_back_as_a_tool_charge(app_client, services, db) -> None:  # type: ignore[no-untyped-def]
    async with app_client(APP) as client:
        body = (await client.post("/v1/ai/chat", json=metered(workflow()))).json()
    reservation_id = body["budget"]["reservation_id"]
    before = (await ledger_spend(db, [reservation_id]))[reservation_id]
    async with services.sessionmaker() as session, session.begin():
        session.add(
            UsageLedgerRow(
                ledger_id=ledger_id(),
                run_id=body["run_id"],
                attempt_id=None,
                tenant_id="gct",
                application_id=APP,
                rate_card_id="rc-tavily-search",
                rate_card_version=services.prices.version,
                charge_type=ChargeType.TOOL_CALL.value,
                measured_or_estimated=Provenance.ESTIMATED.value,
                amount_micros=8_000,
                reservation_id=reservation_id,
            )
        )
    after = (await ledger_spend(db, [reservation_id]))[reservation_id]
    assert after.api_micros == before.api_micros + 8_000  # it counts in the shared scopes
    [tool] = [c for c in after.charges if c.kind == "tool"]
    assert tool == Charge("rc-tavily-search", 8_000, provenance=Provenance.ESTIMATED, kind="tool")
    assert [c for c in after.charges if c.kind == "model"] == before.charges  # model charges unchanged


async def test_a_run_without_tool_calls_reports_none(app_client) -> None:  # type: ignore[no-untyped-def]
    async with app_client(APP) as client:
        cost = (await client.post("/v1/ai/chat", json=metered(workflow()))).json()["cost"]
    assert cost["tool_micros"] == 0 and cost["tool_provenance"] is None
    assert cost["total_api_micros"] == cost["upstream_micros"] + cost["cim_micros"] + cost["baseline_micros"]
