"""Approved budget exceptions (ADR-0014).

A budget denied with `on_exhaustion: approval_required` becomes a `budget` approval request (Govern, ADR-0012)
carrying the denied budget rule ID and, as `approved_amount_micros`, what that budget needed for the request
(its share of the envelope: API spend, or the GPU estimate for a GPU pool). When the request is resubmitted,
Govern claims the approval for the new run and passes its ID on `ReservationRequest.approval_id`.

FinOps never trusts that pointer. It loads the approval itself and applies it only when every rule holds;
anything else is ignored, so the reservation meets the ordinary caps and fails closed. An applied approval
lifts only the caps of the budgets it names, each by at most the approved amount, for that one reservation;
it is consumed atomically with the reservation (see `scripts.RESERVE`) and recorded durably on
`budget_reservation.approval_id` (unique). It changes cap arithmetic only, never eligibility or egress.

No approval is offered when the denying budget already held more than its cap before the request (spent plus
reserved): one for the request's share could never admit it, and it never covers anyone else's overspend. The
denial says so instead (`BUDGET_ALREADY_EXHAUSTED`, ADR-0014 amendment).
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aiva_gateway.db.models import ApprovalRequestRow, BudgetReservationRow

ALREADY_USED = "it has already funded another reservation"
EXCEEDS_APPROVED = "this request now needs more than the approved amount"
# `error.details.reason` of a denial for which no approval is offered, and the copy that leads its message.
BUDGET_ALREADY_EXHAUSTED = "budget_already_exhausted"
BUDGET_ALREADY_EXHAUSTED_COPY = "Budget already exhausted; raise the budget."


@dataclass(frozen=True)
class ApprovedException:
    approval_id: str
    budget_ids: frozenset[str]
    allowance_micros: int


async def verify_budget_approval(
    session: AsyncSession,
    *,
    approval_id: str,
    tenant_id: str,
    application_id: str | None,
    run_id: str,
    now: datetime,
) -> ApprovedException | str:
    """The approval to apply, or why it cannot be applied to this reservation. Whether each named budget's
    share of the reservation fits the approved amount is checked against the reservation (`quota.py`)."""
    row = await session.get(ApprovalRequestRow, approval_id)
    if row is None or row.tenant_id != tenant_id or row.application_id != application_id:
        return "it does not belong to this request"
    if row.kind != "budget":
        return "it is not a budget approval"
    if row.status != "consumed" or row.consumed_by_run_id != run_id:
        return "it was not claimed by this run"
    if row.expires_at <= now:
        return "it has expired"
    if not row.approved_amount_micros or not row.rule_ids:
        return "it names no budget or approved amount"
    # Durable single use; the reserve script's marker and the unique index also cover concurrent uses.
    used = (
        select(BudgetReservationRow.reservation_id).where(BudgetReservationRow.approval_id == row.id).limit(1)
    )
    if await session.scalar(used) is not None:
        return ALREADY_USED
    return ApprovedException(
        approval_id=row.id,
        budget_ids=frozenset(row.rule_ids),
        allowance_micros=row.approved_amount_micros,
    )
