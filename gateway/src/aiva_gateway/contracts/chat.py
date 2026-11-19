"""`POST /v1/ai/chat` contract (LLD §8), extended with the trace fields the Test Console shows (§22.6)."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from aiva_gateway.auth.run_identity import ExecutionMode
from aiva_gateway.contracts.common import ContractModel, ErrorBody
from aiva_gateway.contracts.messages import ChatMessage, EvidenceList, TestProfile
from aiva_gateway.contracts.policy import DecisionReason, DeniedCandidate
from aiva_gateway.contracts.runs import TraceSpanOut
from aiva_gateway.core.enums import (
    AttemptStage,
    CimState,
    Classification,
    ExecutionPath,
    IdentityMode,
    Outcome,
    PolicyAction,
    Provenance,
    RunStatus,
    SourceMode,
)


class RoutingOptions(ContractModel):
    mode: Literal["hybrid", "local-only", "pinned"] = "hybrid"
    tags: list[str] = Field(default_factory=list, description="Hints only; never authorization (LLD §7).")
    preferred_model: str | None = Field(
        default=None, description="Hint; honoured only inside the allowed set."
    )


class ChatRequest(ContractModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=500)
    routing: RoutingOptions = Field(default_factory=RoutingOptions)
    data_classification: Classification | None = Field(
        default=None,
        description="Declared classification. The most restrictive of declared and detected wins.",
    )
    task: str | None = Field(default=None, description="Task category hint, e.g. 'qa', 'analysis'.")
    quality_tier: Literal["standard", "high", "critical"] = "standard"
    max_output_tokens: int = Field(default=1024, ge=1, le=32_768)
    scenario_id: str | None = None
    evidence: EvidenceList = Field(default_factory=list, max_length=200)
    baseline_comparison: bool = Field(
        default=False,
        description="Also run the pinned frontier baseline where policy allows (metered separately).",
    )
    workflow_id: str | None = Field(
        default=None,
        max_length=40,
        pattern=r"^[A-Za-z0-9._-]+$",
        description="Agent workflow ID: runs sharing it draw on one workflow budget envelope (LLD §12).",
    )
    trace: bool = Field(default=False, description="Include the span list in the response.")
    execution_mode: ExecutionMode | None = Field(
        default=None,
        description="Per-run execution mode (ADR-0019). Omitted: replay for public visitors, otherwise the "
        "gateway's configured mode. `replay` uses recorded exchanges only; a replay miss is answered by the "
        "simulated adapter. `live` is refused on a gateway that is not live-capable and needs the live "
        "permission; it is never silently downgraded.",
    )
    test_profile: TestProfile | None = None
    approval_id: str | None = Field(
        default=None,
        max_length=40,
        description="Resume after approval: the approved request's ID. Single-use and bound to the original "
        "request, application and requester. Policy is evaluated again; approval never overrides a deny.",
    )


class ResponseMessage(ContractModel):
    role: Literal["assistant"] = "assistant"
    content: str
    finish_reason: str | None = None
    provenance: Provenance


class ModelUsage(ContractModel):
    """One model call's tokens: a CIM call, an upstream call (a retry or fallback is its own entry, under the
    model that served it) or the baseline comparison."""

    model_id: str
    stage: AttemptStage
    input_tokens: int
    output_tokens: int
    provenance: Provenance


class UsageSummary(ContractModel):
    """Token accounting (LLD §21.4). Denied and cache-hit runs report zero upstream usage."""

    original_context_tokens: int = Field(
        default=0,
        description=(
            "The full context before the CIM prunes anything: the messages and every eligible passage, "
            "supplied or retrieved, rendered as sent. Estimated tokens, like constructed_context_tokens, "
            "so a run that sends the full context reports the same number for both."
        ),
    )
    constructed_context_tokens: int = 0
    upstream_submitted_input_tokens: int = 0
    upstream_output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    cim_input_tokens: int = 0
    cim_output_tokens: int = 0
    retry_tokens: int = 0
    provenance: Provenance
    by_model: list[ModelUsage] = Field(
        default_factory=list,
        description="Every model call of the run that used tokens, in order. The aggregates above are "
        "unchanged; these split tokens by where they were served (local, inference API, frontier).",
    )


class CostSummary(ContractModel):
    upstream_micros: int = 0
    cim_micros: int = 0
    baseline_micros: int = 0
    gpu_estimated_micros: int = Field(
        default=0,
        description="Estimated cost of GPU time on priced endpoints (busy time x hourly rate); "
        "equals the run's `gpu_estimate` ledger charges.",
    )
    gpu_estimated_provenance: Provenance | None = Field(
        default=None,
        description="Label of `gpu_estimated_micros`, never shared with `provenance`: `estimated` when an "
        "endpoint rate produced it, `simulated` under simulated inference; null when no call ran on a priced "
        "GPU endpoint, or only replayed ones (a replay's latency is not the endpoint's busy time): no GPU "
        "estimate applies.",
    )
    tool_micros: int = Field(
        default=0,
        description="Billed tool calls (e.g. web search), priced per billed unit at the tool's rate card and "
        "reserved before they ran; part of `total_api_micros`.",
    )
    tool_provenance: Provenance | None = Field(
        default=None,
        description="Label of `tool_micros`: `measured` when the provider reported the units it billed, "
        "`estimated` when they were inferred, `replayed`/`simulated` without a provider; null with no call.",
    )
    total_api_micros: int = Field(
        default=0,
        description="Provider-billable API cost, billed tool calls included; excludes GPU estimates.",
    )
    direct_frontier_estimated_micros: int | None = Field(
        default=None,
        description="What this request would have cost sent straight to the pinned frontier model with no "
        "AiVa optimization: the full original context (on the served attempt's metered token basis, as the "
        "savings waterfall's frontier baseline) and the served answer's output tokens, at the frontier's "
        "list price with no cache discount. On a cache hit, the frontier price of the reused answer. "
        "Arithmetic only: no call is made, on any path. Null when no answer was produced. The measured "
        "comparison is `baseline_micros`.",
    )
    direct_frontier_provenance: Provenance | None = Field(
        default=None,
        description="Label of `direct_frontier_estimated_micros`: `estimated`, or `replayed`/`simulated` "
        "when the tokens it is priced on are; never `measured`. Null with the amount.",
    )
    currency: Literal["USD"] = "USD"
    rate_card_version: str
    provenance: Provenance = Field(
        description="Label of the API amounts; the GPU estimate carries its own (`gpu_estimated_provenance`)."
    )


class PolicySummary(ContractModel):
    action: PolicyAction
    policy_version: str
    bundle_hash: str
    matched_rule_ids: list[str]
    declared_classification: Classification | None
    effective_classification: Classification
    allowed_models: list[str]
    denied_candidates: list[DeniedCandidate] = Field(default_factory=list)
    reasons: list[DecisionReason] = Field(default_factory=list)


class IdentitySummary(ContractModel):
    application_id: str
    identity_mode: IdentityMode
    source_mode: SourceMode
    matched_network_id: str | None = None


class RoutingSummary(ContractModel):
    rlcd_version: str
    task_category: str | None = None
    complexity: str | None = None
    confidence: float | None = None
    context_strategy: str | None = None
    reasons: list[str] = Field(default_factory=list)


class CacheSummary(ContractModel):
    checked: bool
    hit: bool = False
    cache_type: str | None = None
    upstream_call: bool = Field(description="False on a full response-cache hit: no upstream inference call.")
    avoided_input_tokens: int | None = Field(
        default=None,
        description="On a cache hit, the upstream input tokens the answer's original run used, so the "
        "tokens this request did not spend. Null on a miss.",
    )
    avoided_output_tokens: int | None = Field(
        default=None, description="On a cache hit, the original run's upstream output tokens. Null on a miss."
    )
    avoided_provenance: Provenance | None = Field(
        default=None,
        description="Label of the avoided tokens: estimated (what this request would have spent, taken from "
        "the original run's usage), or simulated or replayed when that run was. Null on a miss.",
    )


class CimSummary(ContractModel):
    state: CimState
    message: str = Field(description="UI copy, e.g. 'CIM bypassed by mandatory sovereignty policy'.")
    cim_revision: str | None = None
    sufficiency: Literal["verified", "cim-selected", "failed-fallback-applied", "not-applicable"] = Field(
        default="not-applicable",
        description=(
            "verified: the Context Guard verified the package, and every passage it leaves out passes the "
            "question-term rule (ADR-0009). failed-fallback-applied: the Guard failed the package, or no "
            "passage the CIM left out passes that rule, so the full context was sent. cim-selected: runs "
            "before 2026-10-22 only, when a package that omitted passages on the CIM's judgement was sent."
        ),
    )
    passages_retrieved: int | None = Field(
        default=None,
        description=(
            "Passages the run's context drew on once retrieval ran: the caller's evidence plus "
            "knowledge-base and web-search passages, one per id. On the CIM-assisted path, the passages the "
            "CIM was given; on the sovereign-direct path, the knowledge-base passages retrieved for the "
            "sovereign model (ADR-0020). Null when no retrieval ran (cache return, local fast path, "
            "sovereign path without retrieval)."
        ),
    )
    passages_kept: int | None = Field(
        default=None,
        description=(
            "Of passages_retrieved, those the package kept: the CIM's selection, the recall floor and the "
            "omissions the question-term rule could not verify, put back. When the package is not verified, "
            "sufficiency is failed-fallback-applied and every passage is sent instead. Null when the CIM "
            "returned no package."
        ),
    )
    passages_omitted: int | None = Field(
        default=None,
        description="passages_retrieved minus passages_kept. Null when the CIM returned no package.",
    )


class SearchSource(ContractModel):
    title: str
    url: str
    evidence_id: str = Field(description="The evidence passage the result became, e.g. `WEB-1#1`.")
    sent_to_model: bool = Field(
        description="The passage was in the payload sent to the answering model. False when the run stopped "
        "before the model call.",
    )


class SearchSummary(ContractModel):
    """Governed live web search (ADR-0022). Only the question was sent; results are untrusted, and any with a
    prompt-injection indicator or a DLP finding was dropped, never redacted and kept. The search's cost is in
    `cost.tool_micros`."""

    tool_id: Literal["web.search"] = "web.search"
    provider: str
    status: Literal["searched", "failed", "denied", "unavailable"] = Field(
        description="searched: the provider answered (`results_used` may be 0). failed: every attempt "
        "failed; the run answered without web evidence. denied: policy refused the call (`rule_ids`). "
        "unavailable: no search on this run (no key, not a live gateway, or a replay or recording run).",
    )
    detail: str | None = Field(default=None, description="UI copy, e.g. 'No search key configured'.")
    attempts: int = Field(default=0, description="Provider calls made: one, or two after a retry.")
    results_returned: int = 0
    results_used: int = Field(
        default=0,
        description="Results that passed screening and became evidence passages `WEB-<n>#1`. How many "
        "reached the answering model is `results_sent`.",
    )
    results_sent: int = Field(
        default=0,
        description="Web passages in the payload sent to the answering model. Every screened passage is kept "
        "whatever the CIM selects, so this equals `results_used` once a model call was made; 0 when the run "
        "stopped before it.",
    )
    results_dropped_injection: int = 0
    results_dropped_dlp: int = 0
    rule_ids: list[str] = Field(default_factory=list, description="Policy rules that denied the call.")
    sources: list[SearchSource] = Field(
        default_factory=list,
        description="Title and URL of each result that passed screening, in evidence order (`WEB-1`, "
        "`WEB-2`, ...), and whether it reached the answering model. In this response only: run records, "
        "spans and reports hold counts and domains.",
    )


class BudgetSummary(ContractModel):
    reservation_id: str | None = None
    reserved_micros: int = 0
    committed_micros: int = 0
    released_micros: int = 0
    provenance: Provenance


class AnswerCitations(ContractModel):
    """Assurance check `answer-citations` (ADR-0009): the answer may cite only what its context package held.
    It reports and never blocks."""

    status: Literal["resolved", "unresolved", "none", "not_checked"] = Field(
        description="resolved: every citation was in the context the model was given. unresolved: at least "
        "one was not (an omitted or made-up passage). none: the answer cites nothing. not_checked: no "
        "upstream payload to check against (cache return, blocked or halted run).",
    )
    cited: int = 0
    unresolved: int = 0
    unresolved_refs: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Unresolved citations: `[DOC-ID §N]` or evidence IDs only, never free text.",
    )


class AssuranceSummary(ContractModel):
    passed: bool
    output_dlp: Literal["clean", "redacted", "blocked", "not-run"]
    checks: list[str] = Field(default_factory=list)
    answer_citations: AnswerCitations = Field(default_factory=lambda: AnswerCitations(status="not_checked"))


class ApprovalSummary(ContractModel):
    """Approval state of the run (LLD §22.8 "Approval required")."""

    approval_id: str | None = Field(
        default=None, description="Set when this run requires approval; resubmit with approval_id."
    )
    status: Literal["pending"] | None = None
    expires_at: datetime | None = None
    rule_ids: list[str] = Field(default_factory=list)
    satisfied_by: str | None = Field(default=None, description="Approval consumed by this resumed run.")


class ChatResponse(ContractModel):
    run_id: str
    trace_id: str
    status: RunStatus
    outcome: Outcome
    selected_model: str | None
    execution_path: ExecutionPath
    cim_invoked: bool
    response: ResponseMessage | None
    usage: UsageSummary
    cost: CostSummary
    policy: PolicySummary
    identity: IdentitySummary
    routing: RoutingSummary | None = None
    cache: CacheSummary
    cim: CimSummary
    budget: BudgetSummary
    assurance: AssuranceSummary
    approval: ApprovalSummary = Field(default_factory=ApprovalSummary)
    search: SearchSummary | None = Field(
        default=None,
        description="Set when the run's context profile searches the web and its retrieval stage ran.",
    )
    data_designation: Literal["synthetic", "actual"]
    execution_mode: Literal["replay", "live", "record", "simulated"] | None = Field(
        default=None,
        description="The mode the run executed in (ADR-0019). Provenance says what each value is: a replay "
        "miss is answered by the simulated adapter and labelled `simulated`.",
    )
    error: ErrorBody | None = None
    trace: list[TraceSpanOut] | None = None
