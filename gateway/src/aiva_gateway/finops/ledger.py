"""Durable FinOps ledger (LLD §12, §13, §21.4, §21.5).

- `budget_reservation` journals every reservation before the first billable call and is the system of
  record the Redis counters are rebuilt from (`bucket_totals`).
- `usage_ledger` is append-only and attempt-level: one API charge per attempt, plus an `estimated` GPU
  charge for calls served on a priced GPU endpoint (none for a replayed call). Corrections are new
  `adjustment` rows.
- `budget_adjustment` is append-only: reconciliation's corrections to a reservation's committed spend.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ARRAY, String, bindparam, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aiva_gateway.catalog import Catalog
from aiva_gateway.core.enums import ChargeType, Provenance
from aiva_gateway.core.errors import NotFoundError, ValidationFailedError
from aiva_gateway.core.ids import ledger_id
from aiva_gateway.db import models as db
from aiva_gateway.finops.pricing import gpu_pools
from aiva_gateway.finops.scopes import commit, least_trusted
from aiva_gateway.pipeline.context import AttemptRecord, RunContext, ToolCharge
from aiva_gateway.pipeline.interfaces import Charge, Reservation, ReservationLine

API_CHARGE_TYPES = tuple(c.value for c in ChargeType if c is not ChargeType.GPU_ESTIMATE)


@dataclass(frozen=True)
class LedgerReservation(Reservation):
    """A reservation held in the live quota store and journaled in Postgres."""

    tenant_id: str = ""
    run_id: str = ""
    idempotency_key: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    allocations: tuple[tuple[str, int], ...] = ()
    approval_id: str | None = None  # the budget approval this reservation consumed (ADR-0014)


def settlement_charges(
    attempts: Sequence[AttemptRecord], tool_charges: Sequence[ToolCharge] = ()
) -> tuple[Charge, ...]:
    """Actual spend for settlement: API cost and estimated GPU cost of every attempt, per model, and the
    billed tool calls, per tool (shared scopes only)."""
    by_model: dict[str, list[AttemptRecord]] = {}
    for attempt in attempts:
        by_model.setdefault(attempt.model.id, []).append(attempt)
    charges = []
    for model_id, group in by_model.items():
        label = least_trusted(a.provenance for a in group)
        assert label is not None
        charges.append(
            Charge(
                model_id=model_id,
                amount_micros=sum(a.cost_micros for a in group),
                gpu_micros=sum(a.gpu_estimate_micros for a in group),
                provenance=label,
            )
        )
    by_tool: dict[str, list[ToolCharge]] = {}
    for call in tool_charges:
        by_tool.setdefault(call.tool_id, []).append(call)
    for tool_id, calls in by_tool.items():
        label = least_trusted(c.provenance for c in calls)
        assert label is not None
        charges.append(Charge(tool_id, sum(c.cost_micros for c in calls), provenance=label, kind="tool"))
    return tuple(charges)


def line_dicts(lines: Iterable[ReservationLine]) -> list[dict[str, Any]]:
    return [asdict(line) for line in lines]


def journal_row(reservation: LedgerReservation, *, pricing_version: str) -> db.BudgetReservationRow:
    return db.BudgetReservationRow(
        reservation_id=reservation.reservation_id,
        run_id=reservation.run_id,
        tenant_id=reservation.tenant_id,
        idempotency_key=reservation.idempotency_key,
        approval_id=reservation.approval_id,
        scope_chain=[list(scope) for scope in reservation.scope_chain],
        lines=line_dicts(reservation.lines),
        allocations={key: {"reserved": amount, "committed": 0} for key, amount in reservation.allocations},
        estimate_micros=reservation.amount_micros,
        status="reserved",
        pricing_version=pricing_version,
        provenance=reservation.provenance.value,
        created_at=reservation.created_at,
    )


async def record_reservation(session: AsyncSession, catalog: Catalog, ctx: RunContext) -> None:
    """Write the run's reservation outcome (upsert: real reservations were journaled at reserve time)."""
    reservation = ctx.reservation
    if reservation is None:
        return
    settled = ctx.reservation_settled
    charges = settlement_charges(ctx.attempts, ctx.tool_charges)
    allocations: dict[str, Any] = {}
    if isinstance(reservation, LedgerReservation):
        commitments = commit(
            dict(reservation.allocations),
            ctx.api_cost_micros,
            charges,
            gpu_pools(catalog),
            reservation.created_at,
        )
        allocations = {
            key: {
                "reserved": c.reserved_micros,
                "committed": c.committed_micros if settled else 0,
                **({"provenance": c.provenance.value} if settled and c.provenance else {}),
            }
            for key, c in commitments.items()
        }
    charge_label = least_trusted(c.provenance for c in charges)
    values = {
        "reservation_id": reservation.reservation_id,
        "run_id": ctx.run_id,
        "tenant_id": ctx.tenant_id,
        "idempotency_key": getattr(reservation, "idempotency_key", None) or None,
        "approval_id": getattr(reservation, "approval_id", None),
        "scope_chain": [list(scope) for scope in reservation.scope_chain],
        "lines": line_dicts(reservation.lines),
        "allocations": allocations,
        "estimate_micros": reservation.amount_micros,
        "actual_micros": ctx.api_cost_micros if settled else 0,
        "released_micros": ctx.released_micros if settled else 0,
        "status": "settled" if settled else "reserved",
        "pricing_version": catalog.rate_cards.version,
        "currency": "USD",
        "provenance": reservation.provenance.value,
        "charge_provenance": charge_label.value if charge_label and settled else None,
        "created_at": getattr(reservation, "created_at", None) or ctx.created_at,
        "settled_at": datetime.now(UTC) if settled else None,
    }
    statement = insert(db.BudgetReservationRow).values(**values)
    updates = {
        name: statement.excluded[name]
        for name in (
            "run_id",
            "allocations",
            "actual_micros",
            "released_micros",
            "status",
            "charge_provenance",
            "settled_at",
        )
    }
    await session.execute(statement.on_conflict_do_update(index_elements=["reservation_id"], set_=updates))


def gpu_estimate_applies(attempt_provenance: Provenance) -> bool:
    """False for a replayed call: its latency is the local replay's, not the endpoint's recorded busy time,
    so no GPU estimate applies until recordings carry busy time."""
    return attempt_provenance != Provenance.REPLAYED


def gpu_charge_provenance(attempt_provenance: Provenance) -> Provenance:
    label = least_trusted([attempt_provenance, Provenance.ESTIMATED])
    assert label is not None
    return label


def gpu_estimate(
    attempts: Iterable[AttemptRecord], pools: Mapping[str, str]
) -> tuple[int, Provenance | None]:
    """A run's estimated GPU cost exactly as `usage_rows` charges it, and the label of that amount.

    The label covers every call served on a priced GPU endpoint (`pools`), including one whose estimate
    rounds to $0. It is None when no such call ran, or only replayed ones: no GPU estimate applies.
    """
    attempts = list(attempts)
    total = sum(a.gpu_estimate_micros for a in attempts)
    label = least_trusted(
        gpu_charge_provenance(a.provenance)
        for a in attempts
        if a.model.id in pools and gpu_estimate_applies(a.provenance)
    )
    return total, label


def usage_rows(ctx: RunContext, catalog: Catalog) -> list[db.UsageLedgerRow]:
    """Attempt-level charges: the API charge of every attempt and the estimated GPU cost of local calls,
    plus one `tool_call` row per billed tool call."""
    now = datetime.now(UTC)
    reservation_id = ctx.reservation.reservation_id if ctx.reservation else None
    rows = []
    for attempt in ctx.attempts:
        common = {
            "run_id": ctx.run_id,
            "attempt_id": attempt.attempt_id,
            "tenant_id": ctx.tenant_id,
            "application_id": ctx.identity.application_id,
            "rate_card_id": attempt.model.rate_card_id,
            "rate_card_version": catalog.rate_cards.version,
            "reservation_id": reservation_id,
            "reconciled_at": now,
        }
        rows.append(
            db.UsageLedgerRow(
                ledger_id=ledger_id(),
                charge_type=attempt.charge_type.value,
                measured_or_estimated=attempt.provenance.value,
                amount_micros=attempt.cost_micros,
                **common,
            )
        )
        if attempt.gpu_estimate_micros:
            rows.append(
                db.UsageLedgerRow(
                    ledger_id=ledger_id(),
                    charge_type=ChargeType.GPU_ESTIMATE.value,
                    measured_or_estimated=gpu_charge_provenance(attempt.provenance).value,
                    amount_micros=attempt.gpu_estimate_micros,
                    **common,
                )
            )
    for call in ctx.tool_charges:
        card = catalog.tool_rate_card(call.tool_id)
        rows.append(
            db.UsageLedgerRow(
                ledger_id=ledger_id(),
                run_id=ctx.run_id,
                attempt_id=None,  # a tool call is not a model attempt
                tenant_id=ctx.tenant_id,
                application_id=ctx.identity.application_id,
                rate_card_id=card.id if card else None,
                rate_card_version=catalog.rate_cards.version,
                charge_type=ChargeType.TOOL_CALL.value,
                measured_or_estimated=call.provenance.value,
                amount_micros=call.cost_micros,
                reservation_id=reservation_id,
                reconciled_at=now,
            )
        )
    return rows


@dataclass(frozen=True)
class BucketTotals:
    spent: int = 0
    reserved: int = 0
    labels: frozenset[str] = frozenset()


_KEYS = bindparam("keys", type_=ARRAY(String))

_RESERVATION_TOTALS = text(
    """
    SELECT a.key AS bucket,
           COALESCE(SUM(CASE WHEN r.status = 'reserved'
                             THEN (a.value ->> 'reserved')::bigint END), 0) AS reserved,
           COALESCE(SUM(CASE WHEN r.status = 'settled'
                             THEN (a.value ->> 'committed')::bigint END), 0) AS spent,
           array_remove(array_agg(DISTINCT CASE WHEN r.status = 'settled'
                                                THEN a.value ->> 'provenance' END), NULL) AS labels
    FROM budget_reservation r CROSS JOIN LATERAL jsonb_each(r.allocations) AS a(key, value)
    WHERE r.tenant_id = :tenant AND r.allocations ?| :keys AND a.key = ANY(:keys)
    GROUP BY a.key
    """
).bindparams(_KEYS)

_ADJUSTMENT_TOTALS = text(
    """
    SELECT bucket, SUM(delta_micros) AS delta
    FROM budget_adjustment
    WHERE tenant_id = :tenant AND bucket = ANY(:keys)
    GROUP BY bucket
    """
).bindparams(_KEYS)


async def bucket_totals(
    session: AsyncSession, tenant_id: str, keys: Sequence[str]
) -> dict[str, BucketTotals]:
    """Spent (settled + adjustments) and held (open reservations) per bucket, from the durable journal."""
    if not keys:
        return {}
    params = {"tenant": tenant_id, "keys": list(keys)}
    spent: dict[str, int] = {}
    reserved: dict[str, int] = {}
    labels: dict[str, frozenset[str]] = {}
    for row in await session.execute(_RESERVATION_TOTALS, params):
        spent[row.bucket] = int(row.spent)
        reserved[row.bucket] = int(row.reserved)
        labels[row.bucket] = frozenset(row.labels or ())
    for row in await session.execute(_ADJUSTMENT_TOTALS, params):
        spent[row.bucket] = spent.get(row.bucket, 0) + int(row.delta)
    return {
        key: BucketTotals(spent.get(key, 0), reserved.get(key, 0), labels.get(key, frozenset()))
        for key in keys
    }


async def post_ledger_adjustment(
    session: AsyncSession,
    catalog: Catalog,
    *,
    run_id: str,
    amount_micros: int,
    reason: str,
    actor_id: str,
    attempt_id: str | None = None,
    provenance: Provenance = Provenance.MEASURED,
) -> db.UsageLedgerRow:
    """Append a correction to the usage ledger (never an UPDATE). Reconciliation carries it into budgets."""
    run = await session.get(db.ExecutionRunRow, run_id)
    if run is None:
        raise NotFoundError(f"Run {run_id} was not found.")
    reservation_id = await session.scalar(
        select(db.BudgetReservationRow.reservation_id).where(db.BudgetReservationRow.run_id == run_id)
    )
    rate_card_id = None
    if attempt_id is not None:
        attempt = await session.get(db.ModelAttemptRow, attempt_id)
        if attempt is None or attempt.run_id != run_id:
            raise ValidationFailedError(f"Attempt {attempt_id} does not belong to run {run_id}.")
        model = catalog.model(attempt.model_id)
        rate_card_id = model.rate_card_id if model else None
    row = db.UsageLedgerRow(
        ledger_id=ledger_id(),
        run_id=run_id,
        attempt_id=attempt_id,
        tenant_id=run.tenant_id,
        application_id=run.application_id,
        rate_card_id=rate_card_id,
        rate_card_version=catalog.rate_cards.version,
        charge_type=ChargeType.ADJUSTMENT.value,
        measured_or_estimated=provenance.value,
        amount_micros=amount_micros,
        reservation_id=reservation_id,
    )
    session.add(row)
    session.add(
        db.AuditLogRow(
            tenant_id=run.tenant_id,
            actor_kind="user",
            actor_id=actor_id,
            action="ledger.adjustment",
            target_type="run",
            target_id=run_id,
            details={"amount_micros": amount_micros, "reason": reason, "attempt_id": attempt_id},
        )
    )
    return row


def allocations_from_row(row: db.BudgetReservationRow) -> dict[str, int]:
    return {key: int(value.get("reserved", 0)) for key, value in row.allocations.items()}


def committed_from_row(row: db.BudgetReservationRow) -> dict[str, int]:
    return {key: int(value.get("committed", 0)) for key, value in row.allocations.items()}


def lines_from_row(row: db.BudgetReservationRow) -> tuple[ReservationLine, ...]:
    return tuple(ReservationLine(**line) for line in row.lines)


def reservation_from_row(row: db.BudgetReservationRow) -> LedgerReservation:
    return LedgerReservation(
        reservation_id=row.reservation_id,
        amount_micros=row.estimate_micros,
        scope_chain=tuple((str(s[0]), str(s[1])) for s in row.scope_chain),
        provenance=Provenance(row.provenance),
        lines=lines_from_row(row),
        tenant_id=row.tenant_id,
        run_id=row.run_id,
        idempotency_key=row.idempotency_key or "",
        created_at=row.created_at,
        allocations=tuple(allocations_from_row(row).items()),
        approval_id=row.approval_id,
    )
