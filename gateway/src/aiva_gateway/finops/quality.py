"""Budget and metering of a presenter's answer-quality comparison (ADR-0023 §6).

A comparison (`POST /v1/runs/{run_id}/quality-check`) makes two billable calls after a live answer: the
frontier reference and the grader. It is budgeted on its own, never inside the served run:

- **Envelope.** One line per model at the worst case of one attempt (`quality_envelope`): the reference at the
  full untrimmed input bound and its own output cap, the grader at a fixed maximum output. No retries.
- **Reservation.** Reserved at request time against the run's scopes in the live domain
  (`quality_reservation_request`): its tenant, department, application and user budgets and the request cap,
  never a workflow, the public-demo scope or the GPU pool. The idempotency key is `qc:{run_id}`, so a
  repeated request gets the same reservation back and never holds or spends twice. A refused envelope means
  "comparison not reserved": nothing is called. Reference and grader lines on a sovereign run exist only when
  policy allowed both calls under the exception; otherwise `covers()` refuses them as for any run.
- **Metering.** Each call is a model attempt (stages `quality_reference` / `quality_grading`) and one ledger
  row on the original `run_id` with the comparison's own `reservation_id` (`quality_ledger_rows`). Rows are
  labelled with the attempt's provenance (`measured` from provider usage) and are measurement cost, never
  savings. Settle with `quota.settle(reservation, actual, charges=settlement_charges(attempts))`.
- **Direct frontier.** `direct_frontier_measured_micros` is the reference's cost: what the frontier actually
  charged for the request on its full context. The estimate (`finops.frontier`) stays beside it.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from aiva_gateway.catalog import Catalog
from aiva_gateway.catalog.models import ModelSpec
from aiva_gateway.core.enums import AttemptStage, ChargeType
from aiva_gateway.core.ids import ledger_id
from aiva_gateway.db import models as db
from aiva_gateway.finops.envelope import Envelope
from aiva_gateway.finops.pricing import PriceBook
from aiva_gateway.finops.scopes import LIVE, scope_chain
from aiva_gateway.pipeline.context import AttemptRecord
from aiva_gateway.pipeline.interfaces import Reservation, ReservationLine, ReservationRequest

QUALITY_CHARGE_TYPES = {
    AttemptStage.QUALITY_REFERENCE: ChargeType.QUALITY_REFERENCE,
    AttemptStage.QUALITY_GRADING: ChargeType.QUALITY_GRADING,
}


def quality_idempotency_key(run_id: str) -> str:
    """One comparison per run (ADR-0023): a repeated request reuses this reservation."""
    return f"qc:{run_id}"


def quality_envelope(
    prices: PriceBook,
    *,
    reference: ModelSpec,
    reference_input_tokens: int,
    reference_max_output_tokens: int,
    grader: ModelSpec | None,
    grader_input_tokens: int = 0,
    grader_max_output_tokens: int = 0,
) -> Envelope:
    """The worst case: one attempt of each call, no retries. Without a grader, the reference only."""
    calls = [(reference, reference_input_tokens, reference_max_output_tokens)]
    if grader is not None:
        calls.append((grader, grader_input_tokens, grader_max_output_tokens))
    amounts: dict[str, int] = {}
    for model, input_tokens, max_output_tokens in calls:
        cost = prices.worst_case_micros(
            model, input_tokens=input_tokens, max_output_tokens=max_output_tokens, attempts=1
        )
        amounts[model.id] = amounts.get(model.id, 0) + cost
    lines = tuple(
        ReservationLine(
            model_id=model_id,
            amount_micros=amount,
            gpu_pool=prices.pool_of(model_id),
            gpu_micros=prices.gpu_worst_case_micros(model_id, attempts=1),
        )
        for model_id, amount in amounts.items()
    )
    return Envelope(amount_micros=sum(amounts.values()), lines=lines, upstream_micros={})


def quality_reservation_request(run: db.ExecutionRunRow, envelope: Envelope) -> ReservationRequest:
    """The comparison's reservation: the run's scopes, in the live domain (comparisons are live only)."""
    chain = scope_chain(
        tenant_id=run.tenant_id,
        department_id=run.department_id,
        application_id=run.application_id,
        principal_pseudonym=run.principal_pseudonym,
        workflow_id=None,
    )
    return ReservationRequest(
        run_id=run.run_id,
        tenant_id=run.tenant_id,
        scope_chain=chain,
        amount_micros=envelope.amount_micros,
        idempotency_key=quality_idempotency_key(run.run_id),
        domain=LIVE,
        lines=envelope.lines,
    )


def quality_ledger_rows(
    run: db.ExecutionRunRow,
    attempts: Sequence[AttemptRecord],
    reservation: Reservation,
    catalog: Catalog,
) -> list[db.UsageLedgerRow]:
    """One `quality_reference` / `quality_grading` row per attempt, on the original run."""
    now = datetime.now(UTC)
    rows = []
    for attempt in attempts:
        charge = QUALITY_CHARGE_TYPES.get(attempt.stage)
        if charge is None:
            raise ValueError(f"not a comparison attempt: stage {attempt.stage.value}")
        rows.append(
            db.UsageLedgerRow(
                ledger_id=ledger_id(),
                run_id=run.run_id,
                attempt_id=attempt.attempt_id,
                tenant_id=run.tenant_id,
                application_id=run.application_id,
                rate_card_id=attempt.model.rate_card_id,
                rate_card_version=catalog.rate_cards.version,
                charge_type=charge.value,
                measured_or_estimated=attempt.provenance.value,
                amount_micros=attempt.cost_micros,
                reservation_id=reservation.reservation_id,
                reconciled_at=now,
            )
        )
    return rows


def direct_frontier_measured_micros(attempts: Sequence[AttemptRecord]) -> int | None:
    """What the frontier charged for the request on its full context: the reference's cost, or None when no
    reference answer was produced."""
    reference = [a for a in attempts if a.stage is AttemptStage.QUALITY_REFERENCE and a.status == "ok"]
    return sum(a.cost_micros for a in reference) if reference else None
