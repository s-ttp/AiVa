"""Administrative contracts: budgets (`/v1/admin/budgets`) and evaluations (`/v1/evaluations/run`)."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from aiva_gateway.contracts.common import ContractModel
from aiva_gateway.core.enums import Provenance


class BudgetIn(ContractModel):
    """A cap on one scope (LLD §12). `scope_id: default` is a template applying to every scope of its type
    without its own budget (e.g. each user, each workflow). Periods: daily/monthly for tenant, department,
    application, user, model and gpu_pool; `request` for request (a per-request ceiling; scope_id is an
    application ID or default); `workflow` for workflow (scope_id `<application_id>/<workflow_id>` or
    default). GPU-pool budgets cap estimated GPU cost; `public_demo` (scope_id: the tenant, daily or monthly)
    caps public visitors' live runs, API plus estimated GPU cost (ADR-0019); `internet_gpu` (scope_id: the
    tenant, daily or monthly) caps the estimated GPU cost of internet key calls' live runs (ADR-0027); all
    others cap API spend."""

    scope_type: Literal[
        "tenant",
        "department",
        "application",
        "user",
        "model",
        "request",
        "workflow",
        "gpu_pool",
        "public_demo",
        "internet_gpu",
    ]
    scope_id: str = Field(min_length=1, max_length=64)
    period: Literal["daily", "monthly", "request", "workflow"]
    cap_micros: int = Field(ge=0)
    alert_thresholds: list[float] = Field(
        default_factory=lambda: [0.8, 0.95],
        max_length=5,
        description="Fractions of the cap that raise an alert once per period; the hard cap is implicit.",
    )
    on_exhaustion: Literal["block", "approval_required", "lower_cost_route"] = "block"


BudgetDomain = Literal["live", "illustrative"]


class BudgetDomainUsage(ContractModel):
    """A budget's usage this period in one money domain (ADR-0008 amendment)."""

    domain: BudgetDomain
    spent_micros: int
    reserved_micros: int
    remaining_micros: int = Field(description="cap - spent - reserved, floored at zero.")
    provenance: Provenance


class BudgetStatus(BudgetIn):
    """Budget feedback (LLD §22.6): cap, spent, reserved, forecast, threshold and remaining."""

    id: str
    period_key: str | None = Field(
        default=None, description="Current period, e.g. 2026-10; null for templates."
    )
    spent_micros: int = Field(description="Committed spend this period, from the ledger.")
    reserved_micros: int = Field(description="Held by in-flight reservations.")
    remaining_micros: int = Field(description="cap - spent - reserved, floored at zero.")
    forecast_micros: int | None = Field(
        default=None, description="Linear run-rate projection to period end (calendar periods only)."
    )
    forecast_provenance: Provenance | None = Field(
        default=None, description="Always `estimated` when a forecast is given."
    )
    thresholds_crossed: list[float] = Field(
        default_factory=list, description="Thresholds committed spend has reached; 1.0 is the hard cap."
    )
    provenance: Provenance = Field(description="Least trustworthy label among the charges counted in spent.")
    domain: BudgetDomain = Field(
        description="Money domain of the figures above (ADR-0008 amendment), that of the gateway's "
        "configured inference mode: `live` counts provider spend of runs that can spend money; "
        "`illustrative` counts replayed and simulated runs against the same cap and is never `measured`.",
    )
    other_domain: BudgetDomainUsage | None = Field(
        default=None, description="The other money domain's usage this period, when it has any."
    )


class ReconciliationSummary(ContractModel):
    reconciliation_id: str
    status: Literal["running", "completed", "failed"]
    started_at: datetime
    completed_at: datetime | None = None


class BudgetList(ContractModel):
    budgets: list[BudgetStatus]
    last_reconciliation: ReconciliationSummary | None = None


class BudgetAlert(ContractModel):
    """A FinOps alert (LLD §21.9). Alerts never replace the enforced reservation and cap."""

    alert_id: str
    kind: Literal[
        "threshold",
        "hard_cap",
        "reservation_overrun",
        "reservation_expired",
        "reconciliation_adjustment",
        "counter_corrected",
        "approved_exception",
        # Insights anomaly rules (LLD §21.9, ADR-0015); scope is an application or a user pseudonym.
        "token_spike",
        "expensive_model_share",
        "repeated_retries",
        "high_output_ratio",
        "unusual_user_consumption",
    ]
    message: str
    budget_id: str | None = None
    scope_type: str | None = None
    scope_id: str | None = None
    domain: BudgetDomain | None = Field(
        default=None, description="Money domain of a budget alert; null for alerts not about a budget."
    )
    period: str | None = None
    period_key: str | None = None
    threshold: float | None = Field(default=None, description="Fraction of the cap; 1.0 is the hard cap.")
    cap_micros: int | None = None
    spent_micros: int | None = None
    amount_micros: int | None = None
    reservation_id: str | None = None
    run_id: str | None = None
    provenance: Provenance
    created_at: datetime


class BudgetAlertList(ContractModel):
    alerts: list[BudgetAlert]


class ReconciliationFinding(ContractModel):
    kind: Literal[
        "settled_from_ledger",
        "expired_orphan",
        "released_redis_orphan",
        "ledger_adjustment",
        "counter_corrected",
    ]
    detail: str
    reservation_id: str | None = None
    bucket: str | None = None
    delta_micros: int | None = None


class ReconciliationReport(ReconciliationSummary):
    """One run of the reservation <-> usage-ledger reconciliation job."""

    triggered_by: str
    reservations_checked: int
    settled: int = Field(description="Open reservations settled from the ledger.")
    expired: int = Field(description="Orphaned holds released.")
    adjustments: int = Field(description="Append-only budget adjustment rows written.")
    counters_corrected: int
    findings: list[ReconciliationFinding]
    error: str | None = None


class EvaluationRunRequest(ContractModel):
    benchmark: Literal[
        "context-effectiveness",
        "routing-effectiveness",
        "confidentiality",
        "finops",
        "cache-safety",
        "cold-start",
        "document-outputs",
    ]
    dataset_revision: str
    scenario_ids: list[str] = Field(default_factory=list)
    model_ids: list[str] = Field(default_factory=list)
    max_cost_micros: int = Field(ge=0, description="Hard spend cap for this evaluation run.")


class EvaluationRun(ContractModel):
    evaluation_id: str
    status: Literal["queued", "running", "completed", "failed"]
    benchmark: str
    dataset_revision: str
