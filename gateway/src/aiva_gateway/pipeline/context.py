"""Mutable per-run state shared by the orchestrator, recorder and response builder."""

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from aiva_gateway.auth.principal import RunIdentity
from aiva_gateway.catalog.models import Application, ContextProfile, ModelSpec, Scenario
from aiva_gateway.contracts.chat import ChatRequest
from aiva_gateway.contracts.cim import CimEvidence, CimRequest
from aiva_gateway.contracts.common import ErrorBody
from aiva_gateway.contracts.policy import PolicyApproval, PolicyClassification, PolicyDecision, PolicyInput
from aiva_gateway.core.enums import (
    AttemptStage,
    ChargeType,
    CimState,
    Classification,
    ExecutionPath,
    Outcome,
    PolicyStage,
    Provenance,
    RunStatus,
)
from aiva_gateway.pipeline.interfaces import (
    AssuranceOutcome,
    CachedResponse,
    CacheKey,
    CimCallResult,
    DlpResult,
    ExecutionPlan,
    GuardResult,
    InferenceResult,
    InitialAssessment,
    Reservation,
    RlcdPrediction,
)


@dataclass
class PolicyEventRecord:
    stage: PolicyStage
    decision: PolicyDecision
    classification: PolicyClassification
    egress_model_id: str | None = None
    reservation_id: str | None = None
    tool_id: str | None = None
    # Kept in memory for approval requests (policy facts only, no prompt text); not persisted per event.
    policy_input: PolicyInput | None = None
    approval_id: str | None = None
    approval_expires_at: datetime | None = None


@dataclass
class BudgetApprovalRecord:
    """A budget reservation denied with `on_exhaustion: approval_required` (ADR-0012)."""

    approval_id: str
    rule_id: str
    message: str
    amount_micros: int
    expires_at: datetime
    policy_input: PolicyInput | None


@dataclass(frozen=True)
class ToolCharge:
    """One billed tool call (e.g. a web search), priced by its tool rate card. Never prompt or result text."""

    call_id: str
    tool_id: str
    units: int  # billed units, e.g. Tavily credits (a basic search is 1, an advanced one 2)
    cost_micros: int
    provenance: Provenance
    at: datetime


@dataclass
class WebSource:
    """A web result that passed screening and became evidence `evidence_id`. `sent`: it was in the payload
    sent to the answering model."""

    evidence_id: str
    title: str
    url: str
    sent: bool = False


@dataclass
class WebSearchRecord:
    """The run's governed web search (ADR-0022). Spans, records and reports take its counts and domains;
    `sources` (titles and URLs) reach the response only."""

    provider: str
    # searched | failed | denied | unavailable
    status: str
    detail: str | None = None
    attempts: int = 0
    results_returned: int = 0
    results_used: int = 0
    dropped_injection: int = 0
    dropped_dlp: int = 0
    rule_ids: list[str] = field(default_factory=list)
    error_code: str | None = None
    domains: list[str] = field(default_factory=list)
    sources: list[WebSource] = field(default_factory=list)

    @property
    def results_sent(self) -> int:
        return sum(source.sent for source in self.sources)


@dataclass
class AttemptRecord:
    attempt_id: str
    stage: AttemptStage
    model: ModelSpec
    started_at: datetime
    completed_at: datetime
    status: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    reasoning_tokens: int
    latency_ms: float | None
    ttft_ms: float | None
    provider_model_version: str
    provenance: Provenance
    error_code: str | None
    retry_of: str | None
    charge_type: ChargeType
    cost_micros: int
    gpu_estimate_micros: int = 0


@dataclass
class RunContext:
    run_id: str
    tenant_id: str
    request: ChatRequest
    identity: RunIdentity
    application: Application | None
    scenario: Scenario | None
    profile: ContextProfile | None
    department_id: str | None
    cost_center_id: str | None
    business_function: str | None
    environment: str
    data_designation: str
    request_hash: str
    messages: list[dict[str, str]]
    # The request's own context (messages and supplied evidence, raw text): RLCD's input.
    original_context_tokens: int
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    perf_start: float = field(default_factory=time.perf_counter)
    trace_id: str = ""

    dlp: DlpResult | None = None
    evidence_dlp: DlpResult | None = None
    classification: PolicyClassification | None = None
    # Approval verified and claimed before the run (resumed request); passed to every policy stage.
    approval: PolicyApproval | None = None
    budget_approval: BudgetApprovalRecord | None = None
    request_decision: PolicyDecision | None = None
    policy_events: list[PolicyEventRecord] = field(default_factory=list)

    # Per-run execution mode (ADR-0019), resolved at ingress: what the run executes in, whether it uses the
    # per-run replay set, and whether its principal is a public visitor (live runs never wake an endpoint).
    execution_mode: str = ""
    per_run_replay: bool = False
    public_visitor: bool = False

    # A model the caller requires (the OpenAI-compatible API's `model`, ADR-0026): served only if it is in the
    # policy-eligible set, else the run is blocked before any billable stage. None: RLCD chooses.
    required_model: str | None = None

    rlcd_prediction: RlcdPrediction | None = None
    assessment: InitialAssessment | None = None
    plan: ExecutionPlan | None = None
    path: ExecutionPath = ExecutionPath.NONE
    cim_state: CimState = CimState.NOT_REACHED
    cim_message: str = "Not reached."
    sufficiency: str = "not-applicable"
    context_strategy: str | None = None

    evidence: list[CimEvidence] = field(default_factory=list)
    # Retrieved passages in retrieval rank order (also in `evidence`, after the supplied ones).
    retrieved: list[CimEvidence] = field(default_factory=list)
    # Governed web search (ADR-0022): set once the profile asks for it; its passages follow the retrieved
    # ones in `evidence`. `planned_evidence_tokens` bounds the input they can add, for a reservation made
    # before the search ran.
    web_search: WebSearchRecord | None = None
    planned_evidence_tokens: int = 0
    # Passages in `evidence` once a retrieval stage ran (supplied and retrieved); None when none ran.
    passages_retrieved: int | None = None
    # The full context's tokens: messages and every eligible passage, supplied or retrieved, before the CIM
    # prunes anything, rendered as sent (`orchestrator.full_context_tokens`). The "before" of context savings.
    context_tokens_before: int = 0
    # Passages the recall floor (`ContextProfile.keep_top_k`) kept after the CIM had omitted them.
    recall_floor_ids: list[str] = field(default_factory=list)
    # Passages the CIM left out that the question-term rule could not verify, put back into the package so it
    # drops only the verified ones (ADR-0009, option A).
    relevance_put_back: list[str] = field(default_factory=list)
    cim_request: CimRequest | None = None
    cim_result: CimCallResult | None = None
    guard: GuardResult | None = None
    upstream_messages: list[dict[str, str]] = field(default_factory=list)
    constructed_context_tokens: int = 0

    reservation: Reservation | None = None
    # The stage the envelope was reserved in: `retrieval` (before a web search), `cim` or `quota`.
    reserved_at: str | None = None
    reservation_settled: bool = False
    budget_notes: list[str] = field(default_factory=list)
    reserved_input_tokens: int = 0
    # The baseline call's output budget, sized once with its reservation line (0 = no baseline reserved).
    baseline_max_output_tokens: int = 0
    released_micros: int = 0
    # Billed tool calls (finops.tools): the worst-case units a stage plans before the reservation, the
    # amount the reservation holds for them, and the calls made.
    planned_tool_units: dict[str, int] = field(default_factory=dict)
    reserved_tool_micros: int = 0
    tool_charges: list[ToolCharge] = field(default_factory=list)
    attempts: list[AttemptRecord] = field(default_factory=list)
    inference: InferenceResult | None = None
    selected_model: ModelSpec | None = None

    cache_checked: bool = False
    cache_key_hash: str | None = None
    cache_scope: str | None = None
    cache_key: CacheKey | None = None
    # Effective classification the cache key was built with (before RLCD can tighten it).
    cache_classification: Classification | None = None
    cache_type: str | None = None
    cache_hit_entry: CachedResponse | None = None
    assurance: AssuranceOutcome | None = None
    response_content: str | None = None
    response_provenance: Provenance = Provenance.SIMULATED

    status: RunStatus = RunStatus.FAILED
    outcome: Outcome = Outcome.ERROR
    error: ErrorBody | None = None
    http_status: int = 200
    approval_id: str | None = None
    completed_at: datetime | None = None

    @property
    def latency_ms(self) -> float:
        return (time.perf_counter() - self.perf_start) * 1000

    @property
    def api_cost_micros(self) -> int:
        return sum(a.cost_micros for a in self.attempts) + self.tool_cost_micros

    @property
    def tool_cost_micros(self) -> int:
        return sum(c.cost_micros for c in self.tool_charges)

    def finish(
        self, status: RunStatus, outcome: Outcome, error: ErrorBody | None = None, http_status: int = 200
    ) -> None:
        self.status, self.outcome, self.error, self.http_status = status, outcome, error, http_status
