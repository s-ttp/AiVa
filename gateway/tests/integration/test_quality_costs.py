"""The answer-quality comparison against the real quota store and ledger (ADR-0023 §6): one reservation per
run however often it is requested, settled in the run's live application budget and never the GPU pool, and
metered as `quality_*` rows on the original run under the comparison's own reservation."""

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from aiva_gateway.core.enums import AttemptStage, ChargeType, Provenance
from aiva_gateway.core.errors import AivaError
from aiva_gateway.db.models import ExecutionRunRow, ModelAttemptRow, UsageLedgerRow
from aiva_gateway.finops.ledger import settlement_charges
from aiva_gateway.finops.quality import quality_envelope, quality_ledger_rows, quality_reservation_request
from aiva_gateway.finops.reconcile import ledger_spend
from aiva_gateway.finops.scopes import LIVE, Bucket, period_key
from aiva_gateway.pipeline.context import AttemptRecord
from tests.integration.test_budgets import APP, metered, workflow
from tests.integration.test_money_domains import spent


def app_bucket() -> Bucket:
    return Bucket("application", APP, "monthly", period_key("monthly", datetime.now(UTC)), LIVE)


def comparison_attempt(services: Any, stage: AttemptStage, model_id: str, cost: int) -> AttemptRecord:
    now = datetime.now(UTC)
    return AttemptRecord(
        attempt_id=f"att_{stage.value}_{now.timestamp():.0f}",
        stage=stage,
        model=services.catalog.model(model_id),
        started_at=now,
        completed_at=now,
        status="ok",
        input_tokens=1200,
        output_tokens=300,
        cached_input_tokens=0,
        reasoning_tokens=0,
        latency_ms=900.0,
        ttft_ms=None,
        provider_model_version="test",
        provenance=Provenance.MEASURED,
        error_code=None,
        retry_of=None,
        charge_type=ChargeType.UPSTREAM_INFERENCE,
        cost_micros=cost,
    )


async def test_a_comparison_is_reserved_once_and_metered_on_its_run(app_client, services, db) -> None:  # type: ignore[no-untyped-def]
    async with app_client(APP) as client:
        body = (await client.post("/v1/ai/chat", json=metered(workflow()))).json()
    run = await db.get(ExecutionRunRow, body["run_id"])
    assert run is not None
    before_run_spend = (await ledger_spend(db, [body["budget"]["reservation_id"]]))[
        body["budget"]["reservation_id"]
    ]

    envelope = quality_envelope(
        services.prices,
        reference=services.catalog.model("claude-flagship"),
        reference_input_tokens=2000,
        reference_max_output_tokens=1000,
        grader=services.catalog.model("gemini-flash-lite"),  # stand-in until the OpenAI grader is pinned
        grader_input_tokens=3000,
        grader_max_output_tokens=300,
    )
    request = quality_reservation_request(run, envelope)
    first = await services.quota.reserve(request)
    again = await services.quota.reserve(request)  # a repeated POST: the same hold, never a second one
    assert again.reservation_id == first.reservation_id and first.amount_micros == envelope.amount_micros

    bucket_before = await spent(services, app_bucket())
    attempts = [
        comparison_attempt(services, AttemptStage.QUALITY_REFERENCE, "claude-flagship", 9_000),
        comparison_attempt(services, AttemptStage.QUALITY_GRADING, "gemini-flash-lite", 300),
    ]
    async with services.sessionmaker() as session, session.begin():
        for sequence, a in enumerate(attempts, start=100):
            session.add(
                ModelAttemptRow(
                    attempt_id=a.attempt_id,
                    run_id=run.run_id,
                    sequence=sequence,
                    stage=a.stage.value,
                    model_id=a.model.id,
                    provider=a.model.provider,
                    provider_model_version=a.provider_model_version,
                    retry_of=None,
                    status=a.status,
                    input_tokens=a.input_tokens,
                    output_tokens=a.output_tokens,
                    cached_input_tokens=0,
                    reasoning_tokens=0,
                    latency_ms=a.latency_ms,
                    ttft_ms=None,
                    provenance=a.provenance.value,
                    error_code=None,
                    started_at=a.started_at,
                    completed_at=a.completed_at,
                )
            )
        await session.flush()
        session.add_all(quality_ledger_rows(run, attempts, first, services.catalog))
    await services.quota.settle(first, 9_300, charges=settlement_charges(attempts))

    assert await spent(services, app_bucket()) - bucket_before == 9_300  # the run's live application budget
    rows = list(
        await db.scalars(select(UsageLedgerRow).where(UsageLedgerRow.reservation_id == first.reservation_id))
    )
    assert {(r.run_id, r.charge_type) for r in rows} == {
        (run.run_id, ChargeType.QUALITY_REFERENCE.value),
        (run.run_id, ChargeType.QUALITY_GRADING.value),
    }
    assert not any(r.charge_type == ChargeType.GPU_ESTIMATE.value for r in rows)  # never the GPU pool
    spend = await ledger_spend(db, [first.reservation_id, body["budget"]["reservation_id"]])
    assert spend[first.reservation_id].api_micros == 9_300
    assert spend[body["budget"]["reservation_id"]] == before_run_spend, "the served run's spend is untouched"
    with pytest.raises(AivaError) as exc:  # a settled comparison never funds a second one
        await services.quota.reserve(request)
    assert exc.value.code == "RESERVATION_ALREADY_SETTLED"
