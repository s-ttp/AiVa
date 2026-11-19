"""The FinOps `workflow` budget of one agent workflow (LLD §12, §15 #6), for the agent's budget readout.

Every model step of a workflow is a governed run sharing one `workflow_id`, so all of them draw on the
lifetime bucket `workflow|<application_id>/<workflow_id>|workflow|lifetime`. This reads that bucket with
the budget that applies to it (its own row, else the `default` template).

`provenance` is the least trustworthy label among the bucket's committed charges and the API charges in
the usage ledger of every run whose reservation drew on it. A $0 simulated step commits nothing to the bucket
but still leaves a `simulated` ledger row, so the readout never shows `measured` over simulated or
estimated spend. GPU estimates are excluded: workflow budgets cap API spend. The bucket is read in the money
domain the workflow's steps run in (ADR-0008 amendment); illustrative spend is never labelled `measured`.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aiva_gateway.core.enums import ChargeType, Provenance
from aiva_gateway.db.models import BudgetReservationRow, UsageLedgerRow
from aiva_gateway.finops.admin import bucket_states
from aiva_gateway.finops.budgets import BudgetDef, load_book
from aiva_gateway.finops.quota import BucketState
from aiva_gateway.finops.scopes import LIVE, MONEY, Bucket, buckets_for, least_trusted
from aiva_gateway.pipeline.interfaces import QuotaService


@dataclass(frozen=True)
class WorkflowBudget:
    scope_id: str  # <application_id>/<workflow_id>
    budget: BudgetDef | None  # None when no workflow budget (and no template) applies
    spent_micros: int  # committed spend of every settled step, plus ledger corrections
    reserved_micros: int  # held by steps still running
    provenance: Provenance

    @property
    def cap_micros(self) -> int | None:
        return self.budget.cap_micros if self.budget else None

    @property
    def on_exhaustion(self) -> str | None:
        return self.budget.on_exhaustion if self.budget else None

    @property
    def remaining_micros(self) -> int | None:
        """Cap less committed spend, never negative (an approved exception can take spend past the cap)."""
        return max(0, self.budget.cap_micros - self.spent_micros) if self.budget else None


def workflow_bucket(application_id: str, workflow_id: str, *, domain: str) -> Bucket:
    [bucket] = buckets_for("workflow", f"{application_id}/{workflow_id}", datetime.now(UTC), domain=domain)
    return bucket


async def workflow_budget(
    quota: QuotaService,
    session: AsyncSession,
    *,
    tenant_id: str,
    application_id: str,
    workflow_id: str,
    domains: Sequence[str],
) -> WorkflowBudget:
    """`domains` lists the money domains the workflow's steps may have run in, most likely first (a
    public visitor's own, for example). Its steps share one domain: the first with activity is read."""
    candidates = [workflow_bucket(application_id, workflow_id, domain=d) for d in domains]
    states = await bucket_states(quota, session, tenant_id, candidates)
    bucket = next((b for b in candidates if _active(states.get(b.key))), candidates[0])
    domain = bucket.domain
    book = await load_book(session, tenant_id)
    state = states.get(bucket.key)
    # Runs whose reservation drew on the bucket (GIN index on allocations); one reservation per run, so
    # their ledger rows are found through the indexed run_id.
    funded = select(BudgetReservationRow.run_id).where(
        BudgetReservationRow.tenant_id == tenant_id,
        BudgetReservationRow.allocations.op("?")(bucket.key),
    )
    charged = await session.scalars(
        select(UsageLedgerRow.measured_or_estimated)
        .distinct()
        .where(
            UsageLedgerRow.tenant_id == tenant_id,
            UsageLedgerRow.run_id.in_(funded),
            UsageLedgerRow.charge_type != ChargeType.GPU_ESTIMATE.value,
        )
    )
    labels = [Provenance(label) for label in charged]
    if state is not None:
        labels.append(state.provenance)
    return WorkflowBudget(
        scope_id=bucket.scope_id,
        budget=book.for_bucket(bucket),
        spent_micros=state.spent if state else 0,
        reserved_micros=state.reserved if state else 0,
        provenance=readout_label(least_trusted(labels), domain),
    )


def _active(state: BucketState | None) -> bool:
    return state is not None and bool(state.spent or state.reserved or state.labels)


def readout_label(label: Provenance | None, domain: str) -> Provenance:
    """Illustrative spend is never presented as money (ADR-0008 amendment)."""
    if domain == LIVE:
        return label or Provenance.MEASURED
    return Provenance.SIMULATED if label is None or label in MONEY else label
