"""FinOps alert records for the Admin Dashboard (LLD §21.9, §22.10).

Alerts inform; they never replace the preflight reservation or the enforced cap. Writing one is best
effort: a failure is logged and the request continues on the enforcement decision already taken.

Writers that re-run over the same window (e.g. anomaly detection) give each draft a deterministic
`alert_id` and record with `on_conflict="ignore"`, so an alert already written is skipped instead of
failing the whole batch.

Budget alerts carry the money domain of their bucket (ADR-0008 amendment). A public visitor's own
illustrative alerts (`visitor:<pseudonym>`) are kept for that visitor's readout and left out of the admin
and Insights alert lists (`admin_alerts`), so visitors' demos cannot flood them.
"""

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import ColumnElement, and_, false, or_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aiva_gateway.core.enums import Provenance
from aiva_gateway.core.ids import new_id
from aiva_gateway.core.money import micros_to_usd
from aiva_gateway.db.base import utcnow
from aiva_gateway.db.models import BudgetAlertRow
from aiva_gateway.finops.budgets import BPS, BudgetDef
from aiva_gateway.finops.scopes import VISITOR, Bucket, application_of

logger = logging.getLogger(__name__)

THRESHOLD = "threshold"
HARD_CAP = "hard_cap"
RESERVATION_OVERRUN = "reservation_overrun"
RESERVATION_EXPIRED = "reservation_expired"
RECONCILIATION_ADJUSTMENT = "reconciliation_adjustment"
COUNTER_CORRECTED = "counter_corrected"
APPROVED_EXCEPTION = "approved_exception"
# Alerts about budget counters, which belong to a money domain; other writers' alerts (e.g. Insights
# anomalies) have none.
BUDGET_KINDS = frozenset(
    {
        THRESHOLD,
        HARD_CAP,
        RESERVATION_OVERRUN,
        RESERVATION_EXPIRED,
        RECONCILIATION_ADJUSTMENT,
        COUNTER_CORRECTED,
        APPROVED_EXCEPTION,
    }
)


def usd(micros: int) -> str:
    return f"${micros_to_usd(micros)}"


def percent(bps: int) -> str:
    return f"{(Decimal(bps) / 100).normalize():f}%"


def scope_label(scope_type: str, scope_id: str) -> str:
    return f"{scope_type.replace('_', ' ')} {scope_id}"


@dataclass(frozen=True)
class AlertDraft:
    tenant_id: str
    kind: str
    message: str
    provenance: Provenance
    budget: BudgetDef | None = None
    bucket: Bucket | None = None
    threshold_bps: int | None = None
    spent_micros: int | None = None
    amount_micros: int | None = None
    reservation_id: str | None = None
    run_id: str | None = None
    alert_id: str | None = None  # deterministic ID for idempotent writers; a fresh ID when None
    application_id: str | None = None  # defaults to the application of the bucket's scope, if any
    domain: str | None = None  # money domain; defaults to the bucket's for budget alerts

    @property
    def money_domain(self) -> str | None:
        if self.domain is not None:
            return self.domain
        return self.bucket.domain if self.bucket and self.kind in BUDGET_KINDS else None

    @property
    def application(self) -> str | None:
        if self.application_id is not None:
            return self.application_id
        return application_of(self.bucket.scope_type, self.bucket.scope_id) if self.bucket else None

    def row(self) -> BudgetAlertRow:
        return BudgetAlertRow(
            alert_id=self.alert_id or new_id("alr"),
            tenant_id=self.tenant_id,
            kind=self.kind,
            budget_id=self.budget.id if self.budget else None,
            scope_type=self.bucket.scope_type if self.bucket else None,
            scope_id=self.bucket.scope_id if self.bucket else None,
            application_id=self.application,
            domain=self.money_domain,
            period=self.bucket.period if self.bucket else None,
            period_key=self.bucket.period_key if self.bucket else None,
            threshold_bps=self.threshold_bps,
            cap_micros=self.budget.cap_micros if self.budget else None,
            spent_micros=self.spent_micros,
            amount_micros=self.amount_micros,
            reservation_id=self.reservation_id,
            run_id=self.run_id,
            message=self.message,
            provenance=self.provenance.value,
            created_at=utcnow(),
        )

    def values(self) -> dict[str, Any]:
        row = self.row()
        return {column.key: getattr(row, column.key) for column in BudgetAlertRow.__table__.columns}


def threshold_alert(
    budget: BudgetDef, bucket: Bucket, bps: int, spent: int, provenance: Provenance, *, run_id: str | None
) -> AlertDraft:
    label = scope_label(bucket.scope_type, bucket.scope_id).capitalize()
    if bps >= BPS:
        message = f"{label} reached the hard cap of its {bucket.period} budget; further spend is denied."
        kind = HARD_CAP
    else:
        message = f"{label} reached {percent(bps)} of its {bucket.period} budget."
        kind = THRESHOLD
    return AlertDraft(
        tenant_id=budget.tenant_id,
        kind=kind,
        message=message,
        provenance=provenance,
        budget=budget,
        bucket=bucket,
        threshold_bps=bps,
        spent_micros=spent,
        run_id=run_id,
    )


def denial_alert(
    budget: BudgetDef,
    bucket: Bucket,
    spent: int,
    requested: int,
    provenance: Provenance,
    *,
    run_id: str | None,
) -> AlertDraft:
    label = scope_label(bucket.scope_type, bucket.scope_id)
    return AlertDraft(
        tenant_id=budget.tenant_id,
        kind=HARD_CAP,
        message=f"Hard cap reached: a reservation of {usd(requested)} was denied by the {bucket.period} "
        f"budget for {label}.",
        provenance=provenance,
        budget=budget,
        bucket=bucket,
        threshold_bps=BPS,
        spent_micros=spent,
        amount_micros=requested,
        run_id=run_id,
    )


def admin_alerts() -> ColumnElement[bool]:
    """Every alert except public visitors' own illustrative ones, which only their visitor sees."""
    return or_(BudgetAlertRow.domain.is_(None), BudgetAlertRow.domain.not_like(f"{VISITOR}:%"))


def alert_visibility(application_ids: Iterable[str], department_id: str | None) -> ColumnElement[bool]:
    """Alerts a non-tenant-wide viewer may see (LLD §21.8): those about one of `application_ids`, and the
    department's own budget alerts. Shared by the budgets API and Insights so both scope alerts alike."""
    alert = BudgetAlertRow
    clauses: list[ColumnElement[bool]] = []
    apps = sorted(set(application_ids))
    if apps:
        clauses.append(alert.application_id.in_(apps))
    if department_id:
        clauses.append(and_(alert.scope_type == "department", alert.scope_id == department_id))
    return or_(*clauses) if clauses else false()


async def record_alerts(
    maker: async_sessionmaker[AsyncSession],
    drafts: Sequence[AlertDraft],
    *,
    on_conflict: Literal["error", "ignore"] = "error",
) -> None:
    """Write alerts in one transaction. With `on_conflict="ignore"`, drafts whose `alert_id` already exists
    (in the table or earlier in the batch) are skipped and the rest are written."""
    if not drafts:
        return
    try:
        async with maker() as session, session.begin():
            if on_conflict == "ignore":
                statement = insert(BudgetAlertRow).on_conflict_do_nothing(index_elements=["alert_id"])
                for draft in drafts:
                    await session.execute(statement.values(**draft.values()))
            else:
                session.add_all(draft.row() for draft in drafts)
    except (SQLAlchemyError, OSError):
        logger.warning("Could not record %d FinOps alert(s)", len(drafts), exc_info=True)


def expired_alert(
    tenant_id: str, *, amount: int, reservation_id: str, run_id: str, domain: str
) -> AlertDraft:
    return AlertDraft(
        tenant_id=tenant_id,
        kind=RESERVATION_EXPIRED,
        message=f"A reservation of {usd(amount)} was released because its run never recorded. "
        "Review provider billing for unrecorded spend.",
        provenance=Provenance.ESTIMATED,
        amount_micros=amount,
        reservation_id=reservation_id,
        run_id=run_id,
        domain=domain,
    )


def adjustment_alert(
    tenant_id: str, *, amount: int, provenance: Provenance, reservation_id: str, run_id: str, domain: str
) -> AlertDraft:
    return AlertDraft(
        tenant_id=tenant_id,
        kind=RECONCILIATION_ADJUSTMENT,
        message=f"A ledger correction of {usd(amount)} was applied to budget counters.",
        provenance=provenance,
        amount_micros=amount,
        reservation_id=reservation_id,
        run_id=run_id,
        domain=domain,
    )


def correction_alert(
    budget: BudgetDef | None,
    bucket: Bucket,
    *,
    tenant_id: str,
    spent: int,
    delta: int,
    provenance: Provenance,
) -> AlertDraft:
    label = scope_label(bucket.scope_type, bucket.scope_id)
    return AlertDraft(
        tenant_id=tenant_id,
        kind=COUNTER_CORRECTED,
        message=f"The live {bucket.period} spend counter for {label} was corrected by {usd(delta)} "
        "to match the ledger.",
        provenance=provenance,
        budget=budget,
        bucket=bucket,
        spent_micros=spent,
        amount_micros=delta,
    )


def approved_exception_alert(
    budget: BudgetDef, bucket: Bucket, *, amount: int, approval_id: str, reservation_id: str, run_id: str
) -> AlertDraft:
    label = scope_label(bucket.scope_type, bucket.scope_id)
    return AlertDraft(
        tenant_id=budget.tenant_id,
        kind=APPROVED_EXCEPTION,
        message=f"An approved budget exception ({approval_id}) let one request reserve {usd(amount)} past "
        f"the {bucket.period} budget for {label}.",
        provenance=Provenance.ESTIMATED,
        budget=budget,
        bucket=bucket,
        amount_micros=amount,
        reservation_id=reservation_id,
        run_id=run_id,
    )
