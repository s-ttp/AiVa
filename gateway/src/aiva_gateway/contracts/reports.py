"""AiVa Insights reporting contracts (LLD §21.6, §21.7, §22.6; ADR-0015).

Money is integer micro-USD. Every figure carries provenance (ADR-0006): provider-measured (actual),
estimated, simulated, replayed and synthetic quantities are reported side by side and never presented as
one another. Avoided usage (full response-cache hits) is modelled and always reported separately from
observed usage. Users appear only as pseudonyms (`usr_…`) or service principals (`service:<application>`).
"""

from datetime import date, datetime
from typing import Literal

from pydantic import Field

from aiva_gateway.contracts.common import ContractModel, PageInfo
from aiva_gateway.core.enums import Provenance

ConsumptionGroup = Literal["application", "department", "user", "category", "model", "provider", "path"]
ClassificationGroup = Literal[
    "business_function", "task_category", "intent", "complexity", "sensitivity", "language"
]
OptimizationGroup = Literal["application", "strategy", "model"]
GovernanceGroup = Literal["policy", "application", "source_policy"]
ReportName = Literal["consumption", "classification", "optimization", "governance"]


class ReportWindow(ContractModel):
    from_date: date = Field(alias="from")
    to_date: date = Field(alias="to", description="Inclusive.")
    timezone: Literal["UTC"] = "UTC"


class ReportFilters(ContractModel):
    """Global filters (LLD §22.6). Row-level scope is always applied as well (LLD §21.8)."""

    department_id: str | None = Field(default=None, max_length=64)
    application_id: str | None = Field(default=None, max_length=64)
    user: str | None = Field(
        default=None, max_length=80, description="A pseudonym (usr_…) or service:<application_id>."
    )
    model_id: str | None = Field(default=None, max_length=64)
    provider: str | None = Field(default=None, max_length=32)
    business_function: str | None = Field(default=None, max_length=32)
    task_category: str | None = Field(default=None, max_length=32)
    intent: str | None = Field(default=None, max_length=64)
    complexity: str | None = Field(default=None, max_length=32)
    sensitivity: str | None = Field(default=None, max_length=32)
    language: str | None = Field(default=None, max_length=16)
    execution_path: str | None = Field(default=None, max_length=32)
    source_policy: str | None = Field(default=None, max_length=64)
    environment: str | None = Field(default=None, max_length=32)
    outcome: str | None = Field(default=None, max_length=32)
    data_designation: str | None = Field(default=None, max_length=16)


class ReportScope(ContractModel):
    """The caller's row-level reporting scope (LLD §21.8)."""

    level: Literal["tenant", "department", "applications"]
    department_id: str | None = None
    application_ids: list[str] = Field(default_factory=list)
    user_identities: Literal["pseudonymous"] = "pseudonymous"
    users_visible: bool = Field(
        default=True,
        description=(
            "False when the user dimension is hidden (the public demo's visitor, ADR-0019, and the tester, "
            "ADR-0021): no user filter or grouping, and run principals are shown as `hidden`."
        ),
    )


class FilterOption(ContractModel):
    value: str
    label: str


class ReportFilterOptions(ContractModel):
    """Values each global filter can take in the window, within the caller's scope (LLD §22.6)."""

    window: ReportWindow
    scope: ReportScope
    options: dict[str, list[FilterOption]] = Field(description="Keyed by filter name, e.g. application_id.")


class ReportMetadata(ContractModel):
    """Report and export metadata (LLD §22.6): filters, period, versions and data designation."""

    window: ReportWindow
    filters: dict[str, str] = Field(default_factory=dict)
    scope: ReportScope
    generated_at: datetime
    aggregates_as_of: datetime | None = Field(
        default=None, description="Runs recorded before this time are included (last completed refresh)."
    )
    granularity: Literal["hour", "day"]
    taxonomy_versions: list[str] = Field(default_factory=list)
    classifier_versions: list[str] = Field(default_factory=list)
    rate_card_versions: list[str] = Field(
        default_factory=list, description="Price snapshots of the charges counted."
    )
    pricing_snapshot: str = Field(description="Price snapshot used for modelled savings.")
    data_designation: Literal["synthetic", "actual", "mixed", "none"]
    attribution: str = "Usage is attributed to the UTC hour its run started."


class Ratio(ContractModel):
    numerator: int
    denominator: int
    value: float | None = Field(description="numerator / denominator; null when the denominator is 0.")


class ProvenanceSplit(ContractModel):
    """A quantity split by where it came from (ADR-0006); `total` is the sum of the parts."""

    measured: int = 0
    estimated: int = 0
    simulated: int = 0
    replayed: int = 0
    synthetic: int = 0
    total: int = 0
    provenance: Provenance = Field(description="The least trustworthy label among non-zero parts.")


class Amount(ContractModel):
    value: int
    provenance: Provenance


class TokenUsage(ContractModel):
    """Attempt-level tokens (LLD §21.4). The categories partition the totals: nothing is counted twice."""

    input_tokens: ProvenanceSplit
    output_tokens: ProvenanceSplit
    upstream_input_tokens: int = Field(description="First calls to the answering model; retries excluded.")
    upstream_output_tokens: int
    cim_input_tokens: int
    cim_output_tokens: int
    retry_input_tokens: int = Field(description="Retried calls of any stage.")
    retry_output_tokens: int
    baseline_input_tokens: int = Field(description="Opt-in frontier baseline comparison calls.")
    baseline_output_tokens: int
    quality_comparison_input_tokens: int = Field(
        default=0,
        description="A presenter's answer-quality comparison: the frontier reference and the grader.",
    )
    quality_comparison_output_tokens: int = 0
    other_input_tokens: int = 0
    other_output_tokens: int = 0
    cached_input_tokens: int = Field(description="Provider prompt-cache tokens (a discount, not avoidance).")
    reasoning_tokens: int


class Spend(ContractModel):
    """Provider-billable API charges from the usage ledger, plus the separate local GPU estimate."""

    api: ProvenanceSplit
    upstream_micros: int = Field(description="First calls to the answering model.")
    cim_micros: int
    retry_micros: int
    baseline_comparison_micros: int = Field(description="Metered separately; never counted in savings.")
    adjustment_micros: int = Field(description="Append-only ledger corrections.")
    tool_call_micros: int = Field(description="Tool calls a provider bills, such as web searches.")
    quality_comparison_micros: int = Field(
        default=0,
        description="A presenter's answer-quality comparison (ADR-0023), metered on the run it compares; a "
        "measurement cost, never counted in savings.",
    )
    other_micros: int = 0
    gpu_estimated: Amount = Field(description="Estimated local GPU cost; not an invoiced amount.")


class AvoidedUsage(ContractModel):
    """Full response-cache hits: no upstream call was made. Modelled from the source answer."""

    calls: int
    tokens: int
    micros: int
    provenance: Provenance


class ReportUsage(ContractModel):
    request_count: int
    provenance: Provenance = Field(
        description="Of the request count: measured at the gateway, or synthetic for generated traffic."
    )
    tokens: TokenUsage
    spend: Spend
    avoided: AvoidedUsage


class OutcomeCounts(ContractModel):
    success: int = 0
    quality_fail: int = 0
    policy_denied: int = 0
    quota_denied: int = 0
    approval_required: int = 0
    timeout: int = 0
    error: int = 0


class CostVariance(ContractModel):
    """Worst-case reservations against committed actual spend (LLD §12): the headroom released."""

    reserved_micros: int
    committed_micros: int
    variance_micros: int
    provenance: Provenance


class SavingsStep(ContractModel):
    step: Literal[
        "frontier_baseline",
        "cache",
        "context",
        "routing",
        "prompt_cache",
        "cim_overhead",
        "retries",
        "tool_calls",
        "adjustments",
        "local_gpu",
        "net",
    ]
    amount_micros: int = Field(description="Signed: reductions are negative, added costs positive.")
    provenance: Provenance


class SavingsWaterfall(ContractModel):
    """Estimated savings versus the pinned frontier baseline (LLD §21.4, §22.7). Components are separated
    so nothing is double counted. The net is the cost of serving: actual API spend and the local GPU
    estimate, except the opt-in baseline comparison, which is a measurement cost shown after the net."""

    frontier_model_id: str
    pricing_snapshot: str
    quality_adjusted: bool = Field(default=False, description="Not quality-adjusted: see BaselineComparison.")
    steps: list[SavingsStep]
    net_savings_micros: int
    measurement_cost: Amount = Field(
        description="Calls that measured runs rather than served them: the opt-in baseline comparison and a "
        "presenter's answer-quality comparison (ADR-0023). Metered, shown after the net and never subtracted "
        "from the savings. The net plus this equals all actual API spend plus the GPU estimate."
    )
    provenance: Provenance


class BaselineComparison(ContractModel):
    """The opt-in, metered comparison with the pinned frontier model on the same submitted payload."""

    frontier_model_id: str
    frontier_model_name: str = Field(description="Display name of the pinned frontier model.")
    runs: int
    baseline: Amount
    actual: Amount
    savings: Amount
    quality_passed_runs: int
    quality_failed_runs: int
    quality_gated_savings: Amount = Field(description="Savings over runs whose own output passed assurance.")
    baseline_quality: Literal["not_graded"] = Field(
        default="not_graded", description="Baseline outputs are not graded in this phase."
    )


class TrendPoint(ContractModel):
    bucket: datetime
    request_count: int
    input_tokens: int
    output_tokens: int
    cim_tokens: int
    retry_tokens: int
    baseline_tokens: int
    quality_comparison_tokens: int = 0
    api_micros: int
    gpu_estimated_micros: int
    avoided_micros: int


FlowStop = Literal["policy_denied", "approval_required", "quota_denied", "not_routed"]
FlowRoute = Literal["cache", "sovereign", "local_fast", "cim_packaged", "full_context"]


class FlowStopCount(ContractModel):
    bucket: FlowStop
    count: int


class FlowRouteCount(ContractModel):
    bucket: FlowRoute
    count: int


class TrafficFlow(ContractModel):
    """Where the requests went: stopped before any route, or the route they took. The buckets partition
    the requests (`requests` = the stopped counts plus the routed counts); both lists keep a fixed order
    and zeros."""

    requests: int
    stopped: list[FlowStopCount] = Field(
        description="No route taken: refused by policy, held for approval, over quota, or failed before "
        "routing."
    )
    routed: list[FlowRouteCount] = Field(
        description="The route taken: response cache, sovereign direct, local fast, the CIM's package "
        "(`cim_packaged`) or full context. A run that failed after routing counts under its route."
    )
    provenance: Provenance = Field(description="Of the counts: measured at the gateway, or synthetic.")


class PackingSummary(ContractModel):
    """What the CIM's packing did, on the requests that sent its package (the flow's `cim_packaged`): how
    many of the passages it was given it kept, and the input tokens the answering model read. Passages are
    counted with the run view's rule (`optimize.cim.passage_counts`), so the two always agree."""

    packed_runs: int
    passages_retrieved: int = Field(description="Passages the CIM was given: supplied and retrieved.")
    passages_kept: int = Field(description="Of those, the passages its package kept, recall floor included.")
    model_input_tokens: ProvenanceSplit = Field(
        description="Input tokens the answering model read on those requests (first calls; retries are "
        "reported with the retries)."
    )
    provenance: Provenance = Field(description="Of the counts: measured at the gateway, or synthetic.")


class CostKpi(ContractModel):
    """AiVa's route against sending the same requests straight to the pinned frontier model, on the requests
    priced both ways: the /live "This session" cost counter, summed over the window."""

    priced_requests: int = Field(
        description="Requests with a direct-frontier estimate: an answer was served, or a full "
        "response-cache hit returned one. Blocked and failed requests are not priced, nor is a cache hit "
        "whose source run is no longer on record."
    )
    aiva: Amount = Field(
        description="AiVa's answering-model, context-packer (CIM) and billed tool-call (web search) spend on "
        "those requests, at the rate cards. Excludes in-house model hosting (`hosting_excluded`), ledger "
        "corrections and measurement calls."
    )
    direct_frontier: Amount = Field(
        description="FinOps' direct-frontier estimate of the same requests (`cost.direct_frontier_estimated_"
        "micros`): the full original context and the served answer at the frontier's list price; a cache hit "
        "avoids the frontier cost of the answer it reused. Never better than estimated."
    )
    saved: Ratio = Field(description="(direct_frontier - aiva) / direct_frontier; null with nothing priced.")
    hosting_excluded: Amount = Field(
        description="Estimated local GPU hosting of the in-house models (CIM and sovereign) in the window, "
        "not in `aiva`, as on /live. The savings waterfall's net includes it, with ledger corrections, so "
        "its net savings differ from `saved` by these."
    )
    provenance: Provenance


class TokensKpi(ContractModel):
    """Tokens the answering models were spared: the /live "This session" token counter, summed."""

    base_tokens: int = Field(
        description="What the answering models would have read and written without AiVa's caching and "
        "context optimization: each answered request's original context and answer, and what each full "
        "cache hit avoided. Blocked and failed requests add 0."
    )
    caching_tokens: int = Field(description="Avoided by full response-cache hits (input and output).")
    context_tokens: int = Field(description="Pruned from answered requests' original context by the CIM.")
    fewer: Ratio = Field(description="(caching_tokens + context_tokens) / base_tokens.")
    caching_provenance: Provenance
    context_provenance: Provenance = Field(description="Context sizes are token estimates: never measured.")
    provenance: Provenance


class QualityKpi(ContractModel):
    """Answer quality against the frontier (ADR-0023): the /live quality counter, over the window."""

    compared_requests: int = Field(description="Requests a presenter compared that the grader scored.")
    mean_score: float | None = Field(
        description="Mean AI-judged score out of 100 over those requests, one per request (its latest "
        "comparison); null when none was compared, never 0."
    )
    cost: Amount = Field(
        description="The comparisons' reference and grader calls, from the ledger: measurement cost, "
        "never savings."
    )
    provenance: Provenance = Field(description="AI-judged: estimated at best.")


class GovernanceKpi(ContractModel):
    """Every request passed AI-PEL."""

    requests: int = Field(description="Requests governed: every request accepted at ingress.")
    stopped_by_policy: int = Field(
        description="Blocked by AI policy or held for approval (outcomes policy_denied and "
        "approval_required)."
    )
    sensitive_requests: int = Field(description="Confidential-or-above requests.")
    sensitive_on_sovereign: int = Field(
        description="Of those, requests served on the sovereign direct path (none reached CIM or an external "
        "provider: GovernanceRow.confidential_external_attempts)."
    )
    provenance: Provenance


class ValueKpis(ContractModel):
    """The Story mode's hero figures (/week, owner 2026-10-22), on the same definitions as /live."""

    cost: CostKpi
    tokens: TokensKpi
    quality: QualityKpi
    governance: GovernanceKpi


class OverviewReport(ContractModel):
    metadata: ReportMetadata
    usage: ReportUsage
    outcomes: OutcomeCounts
    flow: TrafficFlow
    packing: PackingSummary
    cache_hit_rate: Ratio
    quality_pass_rate: Ratio
    cost_variance: CostVariance
    savings: SavingsWaterfall
    baseline: BaselineComparison
    value_kpis: ValueKpis
    trend: list[TrendPoint]


class ReportPage(PageInfo):
    total_rows: int


class ConsumptionRow(ContractModel):
    group: str
    label: str
    usage: ReportUsage
    spend_share: Ratio


class SeriesPoint(ContractModel):
    bucket: datetime
    group: str
    request_count: int
    input_tokens: int
    output_tokens: int
    cim_tokens: int
    retry_tokens: int
    api_micros: int


class ConsumptionReport(ContractModel):
    metadata: ReportMetadata
    group_by: ConsumptionGroup
    rows: list[ConsumptionRow]
    totals: ReportUsage
    series: list[SeriesPoint] = Field(description="Trend of the groups on this page.")
    page: ReportPage


class ModelSuitability(ContractModel):
    model_id: str
    request_count: int
    quality_checked: int
    quality_passed: int


class ClassificationRow(ContractModel):
    group: str
    label: str
    request_count: int
    share: Ratio
    tokens: int
    api: Amount
    low_confidence: bool = Field(description="True for the unclassified / review-needed bucket.")
    models: list[ModelSuitability]


class ClassificationTrendPoint(ContractModel):
    bucket: datetime
    group: str
    request_count: int


class TaxonomyInfo(ContractModel):
    version: str
    review_confidence_threshold: float


class ClassificationReport(ContractModel):
    metadata: ReportMetadata
    group_by: ClassificationGroup
    rows: list[ClassificationRow]
    trend: list[ClassificationTrendPoint]
    unclassified: Ratio
    taxonomy: TaxonomyInfo


class LatencySummary(ContractModel):
    average_attempt_ms: float | None
    average_run_ms: float | None
    attempt_samples: int


class CimOverhead(ContractModel):
    input_tokens: int
    output_tokens: int
    api: Amount
    gpu_estimated: Amount


class RetrySummary(ContractModel):
    retries: int
    failed_attempts: int
    attempts: int
    tokens: int
    api: Amount


class OptimizationRow(ContractModel):
    group: str
    label: str
    request_count: int
    cim_invoked: int
    cim_bypassed_by_policy: int
    cim_skipped: int
    local_fast: int
    sovereign_direct: int
    cache_hits: int
    context_tokens_before: int
    context_tokens_after: int
    context_reduction: Ratio
    packing: PackingSummary
    cached_input_tokens: int
    avoided: AvoidedUsage
    cim_overhead: CimOverhead
    retries: RetrySummary
    latency: LatencySummary
    savings: SavingsWaterfall


class OptimizationReport(ContractModel):
    metadata: ReportMetadata
    group_by: OptimizationGroup
    rows: list[OptimizationRow]
    packing: PackingSummary
    savings: SavingsWaterfall
    baseline: BaselineComparison


class RuleCount(ContractModel):
    rule_id: str
    count: int


class GovernanceRow(ContractModel):
    group: str
    label: str
    request_count: int
    allowed: int
    policy_denied: int
    approval_required: int
    quota_denied: int
    failed: int
    confidential_requests: int
    sovereign_direct: int
    cim_bypassed_by_policy: int
    confidential_external_attempts: int = Field(
        description="Must be 0: confidential data never leaves on a served path (CIM, upstream, baseline)."
    )
    confidential_cim_attempts: int = Field(description="Must be 0: confidential data never reaches CIM.")
    confidential_comparison_attempts: int = Field(
        default=0,
        description="Answer-quality comparison calls on confidential requests: allowed only under ADR-0023's "
        "exception for scenario 11's synthetic sample, and audited per comparison.",
    )
    approval_exceptions: int
    denial_reasons: list[RuleCount]


class AlertCount(ContractModel):
    kind: str
    count: int


class PolicyVersionChange(ContractModel):
    version: str
    published_at: datetime


class GovernanceReport(ContractModel):
    metadata: ReportMetadata
    group_by: GovernanceGroup
    rows: list[GovernanceRow]
    totals: GovernanceRow
    alerts: list[AlertCount]
    policy_versions: list[PolicyVersionChange]


QualityGroup = Literal["application", "context_profile", "execution_path"]
GuardFailureKind = Literal[
    "unknown_reference",
    "selected_and_omitted",
    "empty_selection",
    "required_field_missing",
    "reported_missing_but_present",
    "source_not_selected",
    "value_not_verbatim",
    "value_not_preserved",
    "dangling_citation",
    "citation_lost",
    "citation_header_lost",
    "wrapper_integrity",
    "context_loss",
    "chronology",
    "numeric_fidelity",
    "token_budget_exceeded",
    "other",
]


class OutputDlpCounts(ContractModel):
    """Output DLP outcomes of the runs whose output was checked."""

    clean: int = 0
    redacted: int = 0
    blocked: int = 0
    not_run: int = 0


class NamedCount(ContractModel):
    name: str
    count: int


class AssuranceQuality(ContractModel):
    """Output assurance (LLD §15) of the runs that reached it."""

    checked_runs: int
    passed: Ratio = Field(description="Runs whose output passed assurance, of the checked runs.")
    output_dlp: OutputDlpCounts
    escalations: list[NamedCount] = Field(description="Why assurance withheld an output, by number of runs.")
    checks: list[NamedCount] = Field(description="The checks that ran, by number of runs.")
    provenance: Provenance = Field(description="Least trustworthy label of the checked runs' outputs.")


class SufficiencyQuality(ContractModel):
    """How each run's context was established (ADR-0018): CIM sufficiency, and when CIM did not run."""

    runs: int
    cim_invoked_runs: int
    verified: Ratio = Field(description="The Context Guard verified the package, of CIM-invoked runs.")
    cim_selected: Ratio = Field(
        description=(
            "The CIM chose the passages and the Guard did not verify their relevance, of CIM-invoked runs."
        )
    )
    fallback: Ratio = Field(
        description="The package failed and the full context was sent, of CIM-invoked runs."
    )
    bypassed_by_policy: Ratio = Field(
        description="CIM bypassed by mandatory sovereignty policy, of all runs."
    )
    skipped_sufficient: Ratio = Field(
        description="CIM skipped because the context was already sufficient, of all runs."
    )
    provenance: Provenance


class GuardFailureCount(ContractModel):
    kind: GuardFailureKind
    count: int


class GuardQuality(ContractModel):
    """Context Guard verdicts on CIM packages (ADR-0018). Failure messages are never returned, only their
    kind."""

    packages: int = Field(description="CIM packages the Guard checked.")
    passed: Ratio = Field(description="Packages with no Guard failure, of the checked packages.")
    relevance_unverified_passages: int = Field(
        description="Passages the CIM omitted that the Guard could not verify."
    )
    recall_floor_added_runs: int = Field(description="Runs where the recall floor added passages back.")
    failures: list[GuardFailureCount]
    provenance: Provenance


class EvidenceCoverage(ContractModel):
    """Answer citations that resolve to the context the model was given (resolved ÷ cited, per run)."""

    status: Literal["measured", "not_measured"] = Field(
        description="not_measured until runs with citations have been checked; never reported as 0."
    )
    measured_runs: int
    mean: float | None = Field(default=None, description="Mean per-run coverage of the measured runs.")
    with_unresolved: Ratio | None = Field(
        default=None, description="Measured runs with at least one unresolved citation."
    )
    provenance: Provenance | None = None


class QualityBlocks(ContractModel):
    request_count: int
    assurance: AssuranceQuality
    sufficiency: SufficiencyQuality
    guard: GuardQuality
    evidence_coverage: EvidenceCoverage


class QualityRow(QualityBlocks):
    group: str
    label: str


class QualityTrendPoint(ContractModel):
    bucket: datetime
    request_count: int
    checked_runs: int
    passed_runs: int
    cim_invoked_runs: int
    verified_runs: int


class QualityReport(ContractModel):
    """Quality and assurance outcomes (LLD §15, §21.7, ADR-0018), computed from the run records at request
    time rather than from the aggregates. Same scope, filters and provenance rules as the other reports; no
    user dimension. Each Ratio carries its numerator and denominator: a 0/0 rate is null, never 100%."""

    metadata: ReportMetadata
    computed_at: datetime
    group_by: QualityGroup
    totals: QualityBlocks
    rows: list[QualityRow]
    trend: list[QualityTrendPoint]


class RunSummary(ContractModel):
    run_id: str
    trace_id: str
    created_at: datetime
    application_id: str
    department_id: str
    principal: str
    task_category: str
    sensitivity: str
    execution_path: str
    outcome: str
    model_id: str | None
    input_tokens: int
    output_tokens: int
    api_micros: int
    provenance: Provenance
    data_designation: str


class RunList(ContractModel):
    metadata: ReportMetadata
    runs: list[RunSummary]
    page: ReportPage


class ReportRunFacts(ContractModel):
    run_id: str
    trace_id: str
    tenant_id: str
    application_id: str
    department_id: str | None
    cost_center_id: str | None
    principal_kind: str
    principal: str
    identity_mode: str
    source_mode: str
    source_policy: str | None
    scenario_id: str | None
    status: str
    outcome: str
    execution_path: str
    cim_state: str
    cache_hit: bool
    selected_model: str | None
    effective_classification: str
    policy_version: str | None
    environment: str
    data_designation: str
    provenance: Provenance
    error_code: str | None
    created_at: datetime
    completed_at: datetime | None
    latency_ms: float | None


class ReportClassification(ContractModel):
    taxonomy_version: str
    classifier_version: str
    business_function: str | None
    predicted_task_category: str | None
    intent: str | None
    predicted_complexity: str | None
    sensitivity: str
    language: str
    confidence: float | None
    review_needed: bool
    reviewed: bool
    reported_task_category: str
    reported_complexity: str
    status: Literal["classified", "unclassified", "reviewed"]


class LedgerCharge(ContractModel):
    ledger_id: str
    attempt_id: str | None
    charge_type: str
    amount_micros: int
    provenance: str
    rate_card_version: str
    created_at: datetime


class ReportAttempt(ContractModel):
    attempt_id: str
    stage: str
    model_id: str
    provider: str
    provider_model_version: str
    status: str
    retry_of: str | None
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    reasoning_tokens: int
    latency_ms: float | None
    provenance: str
    error_code: str | None
    charges: list[LedgerCharge]


class ReportPolicyEvent(ContractModel):
    stage: str
    action: str
    policy_version: str
    bundle_hash: str
    matched_rule_ids: list[str]
    effective_classification: str
    egress_model_id: str | None
    tool_id: str | None


class ReportOptimization(ContractModel):
    cache_type: str | None
    cache_hit: bool
    context_strategy: str | None
    context_tokens_before: int
    context_tokens_after: int
    removed_passages: int
    preservation_result: str | None
    avoided_call: bool
    modeled_avoided_tokens: int
    modeled_avoided_micros: int


class ReportValidation(ContractModel):
    assurance_passed: bool | None
    output_dlp: str | None
    checks: list[str]
    context_validation: str | None
    missing_fields: list[str]


class ReportReservation(ContractModel):
    reservation_id: str
    estimate_micros: int
    actual_micros: int
    released_micros: int
    status: str
    provenance: str


class ReportSpan(ContractModel):
    name: str
    status: str
    started_at: datetime
    duration_ms: float
    skip_reason: str | None


class ReportRunDetail(ContractModel):
    """Authorized drilldown of one request (LLD §21.6): attempt-level usage, classification, policy and
    validation. Never prompt or response text."""

    run: ReportRunFacts
    classification: ReportClassification | None
    usage: ReportUsage
    attempts: list[ReportAttempt]
    other_charges: list[LedgerCharge] = Field(description="Ledger corrections and attempt-less charges.")
    policy_events: list[ReportPolicyEvent]
    optimization: ReportOptimization | None
    validation: ReportValidation
    reservation: ReportReservation | None
    spans: list[ReportSpan]


class IdentityResolutionRequest(ContractModel):
    pseudonyms: list[str] = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=10, max_length=500, description="Recorded in the audit log.")


class ResolvedIdentity(ContractModel):
    pseudonym: str
    kind: Literal["directory", "synthetic", "service", "unknown"]
    user_id: str | None = None
    display_name: str | None = None


class IdentityResolution(ContractModel):
    identities: list[ResolvedIdentity]


class ExportRequest(ContractModel):
    report: ReportName
    group_by: str = Field(max_length=32)
    window: ReportWindow
    filters: ReportFilters = Field(default_factory=ReportFilters)


class ExportJob(ContractModel):
    export_id: str
    status: Literal["queued", "running", "completed", "failed", "expired"]
    report: ReportName
    group_by: str
    window: ReportWindow
    filters: dict[str, str] = Field(default_factory=dict)
    created_at: datetime
    completed_at: datetime | None = None
    expires_at: datetime
    row_count: int | None = None
    download_url: str | None = Field(
        default=None, description="Same-origin path; the requester's credential is required to download."
    )
    error: str | None = None


class ExportList(ContractModel):
    exports: list[ExportJob]
