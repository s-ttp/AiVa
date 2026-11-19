"""Answer-quality comparison calls on a confidential run, read from the run records (ADR-0023, ADR-0015).

A comparison call counts apart from the served path only when the run meets the exception's gates its record
holds and the call's own egress policy events, under the comparison's reservation, all allowed it. Each case
runs in a transaction that is rolled back, so no other test sees these rows.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
from sqlalchemy import update

from aiva_gateway.core.ids import attempt_id, ledger_id
from aiva_gateway.db.models import ExecutionRunRow, ModelAttemptRow, PolicyEventRow, UsageLedgerRow
from aiva_gateway.reporting.aggregate import load_run_facts
from aiva_gateway.reporting.lines import QUALITY_EXCEPTION_SCENARIO, run_lines
from aiva_gateway.reporting.totals import Totals
from aiva_gateway.reporting.traffic import SYNTHETIC_USERS, TrafficSpec, generate_traffic
from aiva_gateway.services import Services
from tests.conftest import console
from tests.insights.conftest import aggregator_for
from tests.integration.test_quality_check import compare, comparing, served

RESERVATION = "res_qc_insights_test"
CALLS = (("quality_reference", "claude-flagship", 28_000), ("quality_grading", "gemini-flash-lite", 700))


@pytest.fixture(scope="module")
async def s11_run(services: Services) -> str:
    """A confidential request on scenario 11, served on the sovereign path (synthetic, simulated)."""
    user = next(u for u in SYNTHETIC_USERS if u.application_id == "gct-support-chatbot")
    spec = TrafficSpec(
        user,
        "Summarise this synthetic customer's account and recent care history.",
        scenario_id=QUALITY_EXCEPTION_SCENARIO,
        classification="confidential",
        label="s11",
    )
    report = await generate_traffic(services, plan=[spec], tenant_id="gct")
    [run_id] = report.by_label["s11"]
    return run_id


async def _counts(
    services: Services, run_id: str, *, actions: Sequence[str | None], scenario: str | None = None
) -> tuple[int, int, int]:
    """Add the two comparison calls to the run, each with its own egress event (`None`: no event), and
    return (confidential external attempts, comparison attempts, measurement cost) as the aggregation sees
    them. Nothing is committed."""
    aggregator = aggregator_for(services)
    now = datetime.now(UTC)
    async with services.sessionmaker() as session, session.begin():
        run = await session.get(ExecutionRunRow, run_id)
        assert run is not None and run.execution_path == "sovereign-direct"
        assert (run.effective_classification, run.data_designation) == ("confidential", "synthetic")
        if scenario is not None:
            await session.execute(
                update(ExecutionRunRow).where(ExecutionRunRow.run_id == run_id).values(scenario_id=scenario)
            )
        for sequence, ((stage, model_id, cost), action) in enumerate(zip(CALLS, actions, strict=True), 100):
            attempt = attempt_id()
            model = services.catalog.model(model_id)
            assert model is not None
            session.add(
                ModelAttemptRow(
                    attempt_id=attempt,
                    run_id=run_id,
                    sequence=sequence,
                    stage=stage,
                    model_id=model_id,
                    provider=model.provider,
                    provider_model_version="test",
                    retry_of=None,
                    status="ok",
                    input_tokens=1200,
                    output_tokens=300,
                    cached_input_tokens=0,
                    reasoning_tokens=0,
                    latency_ms=900.0,
                    ttft_ms=None,
                    provenance="measured",
                    error_code=None,
                    started_at=now,
                    completed_at=now,
                )
            )
            await session.flush()
            session.add(
                UsageLedgerRow(
                    ledger_id=ledger_id(),
                    run_id=run_id,
                    attempt_id=attempt,
                    tenant_id=run.tenant_id,
                    application_id=run.application_id,
                    rate_card_id=model.rate_card_id,
                    rate_card_version=services.catalog.rate_cards.version,
                    charge_type=stage,
                    measured_or_estimated="measured",
                    amount_micros=cost,
                    reservation_id=RESERVATION,
                )
            )
            if action is not None:
                session.add(
                    PolicyEventRow(
                        run_id=run_id,
                        sequence=sequence,
                        stage="egress",
                        egress_model_id=model_id,
                        action=action,
                        policy_version="test",
                        bundle_hash="test",
                        effective_classification="confidential",
                        reservation_id=RESERVATION,
                    )
                )
        await session.flush()
        facts = await load_run_facts(session, aggregator.threshold, ExecutionRunRow.run_id == run_id)
        await session.rollback()
    totals = Totals().extend(run_lines(facts[run_id], aggregator.pricing))
    return (
        totals.m("confidential_external_attempts"),
        totals.m("confidential_comparison_attempts"),
        totals.savings("claude-flagship", "v").measurement_cost.value,
    )


async def test_comparison_calls_allowed_under_the_exception_are_reported_apart(services, s11_run) -> None:
    assert await _counts(services, s11_run, actions=("allow", "allow")) == (0, 2, 28_700)


@pytest.mark.parametrize(
    "actions",
    [("allow", "deny"), ("allow", None), (None, None)],
    ids=["a call policy denied", "a call without its own policy event", "no policy events"],
)
async def test_a_comparison_call_its_policy_did_not_allow_counts_as_a_leak(
    services, s11_run, actions
) -> None:
    leaked = sum(action != "allow" for action in actions)
    assert await _counts(services, s11_run, actions=actions) == (leaked, 2 - leaked, 28_700)


async def test_a_comparison_on_another_scenario_counts_as_a_leak(services, s11_run) -> None:
    """The run record's gates are checked apart from policy: an allowed call still leaks off scenario 11."""
    assert await _counts(services, s11_run, actions=("allow", "allow"), scenario="fraud-management") == (
        2,
        0,
        28_700,
    )


async def test_a_denied_scenario_11_comparison_leaves_no_attempts(  # type: ignore[no-untyped-def]
    services, tester_client, make_client, user_token, db
) -> None:
    """End to end: with no exception to AIPEL-INV-001, the comparison is refused and bills nothing."""
    scenario = services.catalog.scenario(QUALITY_EXCEPTION_SCENARIO)
    assert scenario is not None and scenario.sample_evidence
    sample = {
        "messages": [{"role": "user", "content": scenario.sample_prompt}],
        "routing": {"mode": "hybrid", "tags": scenario.routing_tags},
        "data_classification": scenario.declared_classification,
        "scenario_id": scenario.id,
        "evidence": [e.model_dump() for e in scenario.sample_evidence],
        **console(scenario.application_id),
    }
    run_id, answer = await served(tester_client, db, sample)
    svc, _, _ = comparing(services)
    body = (await compare(make_client, user_token, svc, run_id, sample, answer)).json()
    assert (body["status"], body["exception_applied"]) == ("denied", False), body
    aggregator = aggregator_for(services)
    async with services.sessionmaker() as session:
        facts = await load_run_facts(session, aggregator.threshold, ExecutionRunRow.run_id == run_id)
    totals = Totals().extend(run_lines(facts[run_id], aggregator.pricing))
    assert totals.m("confidential_external_attempts") == 0
    assert totals.m("confidential_comparison_attempts") == 0
    assert totals.spend().quality_comparison_micros == 0
