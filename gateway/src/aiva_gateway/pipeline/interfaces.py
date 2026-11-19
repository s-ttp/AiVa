"""Stage interfaces. Owning workstreams replace the P0 placeholder implementations behind these."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from aiva_gateway.catalog.models import ContextProfile, Grounding, ModelSpec
from aiva_gateway.contracts.cim import CimEvidence, CimRequest, CimResponse
from aiva_gateway.contracts.policy import PolicyDecision, PolicyInput
from aiva_gateway.core.enums import Classification, Provenance

# --- Govern ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DlpFinding:
    type: str
    classification: Classification
    start: int
    end: int


@dataclass(frozen=True)
class DlpResult:
    findings: tuple[DlpFinding, ...] = ()
    injection_indicators: tuple[str, ...] = ()
    scanner_version: str = ""
    # The injection detector's version when detection ran ("" when it did not): what a stored result was
    # screened with, so a result from another detector version is never taken as current.
    injection_detector: str = ""

    @property
    def finding_types(self) -> list[str]:
        return sorted({f.type for f in self.findings})

    @property
    def classification(self) -> Classification | None:
        if not self.findings:
            return None
        return max((f.classification for f in self.findings), key=lambda c: c.rank)

    @property
    def injection_detected(self) -> bool:
        return bool(self.injection_indicators)


class DlpScanner(Protocol):
    version: str

    def scan(self, text: str, *, detect_injection: bool = True) -> DlpResult: ...

    def redact(self, text: str, result: DlpResult) -> str: ...


class PolicyEngine(Protocol):
    async def evaluate(self, policy_input: PolicyInput) -> PolicyDecision:
        """Return the decision. Raise DependencyUnavailableError when no safe decision is possible."""
        ...

    async def ready(self) -> bool: ...


# --- Optimize ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SemanticCacheQuery:
    """Present only when the LLD §11 guardrails allow semantic reuse. `text` is embedded, never persisted."""

    text: str
    bucket: str
    entity_signature: str


@dataclass(frozen=True)
class CacheKey:
    key_hash: str
    scope: str
    semantic: SemanticCacheQuery | None = None


@dataclass(frozen=True)
class CachedResponse:
    source_run_id: str
    model_id: str
    content: str
    modeled_input_tokens: int
    modeled_output_tokens: int
    modeled_cost_micros: int
    provenance: Provenance
    cache_type: str = "exact"
    similarity: float | None = None


class ResponseCache(Protocol):
    cache_type: str

    async def lookup(self, key: CacheKey) -> CachedResponse | None: ...

    async def store(self, key: CacheKey, entry: CachedResponse, ttl_seconds: int) -> None: ...


@dataclass(frozen=True)
class RlcdPrediction:
    """Calibrated output of the RLCD classifier (LLD §10): per head, the label order and the probabilities.

    Probabilities are `measured` model outputs; they are inputs to routing, never policy decisions.
    """

    model_version: str
    labels: Mapping[str, tuple[str, ...]]
    probabilities: Mapping[str, tuple[float, ...]]
    latency_ms: float

    def top(self, head: str) -> tuple[str, float]:
        probs = self.probabilities[head]
        index = max(range(len(probs)), key=probs.__getitem__)
        return self.labels[head][index], probs[index]


class RlcdClassifier(Protocol):
    """Runs the RLCD model off the event loop. None means unavailable or over its time budget."""

    version: str

    async def classify(self, messages: list[dict[str, str]]) -> RlcdPrediction | None: ...


@dataclass(frozen=True)
class RouterRequest:
    task: str | None
    scenario_task: str | None
    quality_tier: str
    original_context_tokens: int
    evidence_count: int
    preferred_model: str | None
    routing_mode: str
    prediction: RlcdPrediction | None = None
    # What the answer would be grounded in: the run's full eligible evidence, never the CIM's selection.
    grounding: Grounding = "none"


@dataclass(frozen=True)
class InitialAssessment:
    rlcd_version: str
    task_category: str
    complexity: str
    predicted_sensitivity: Classification | None
    context_sufficient: bool
    local_fast_eligible: bool
    confidence: float | None
    reasons: tuple[str, ...]
    # Calibrated confidence per RLCD head, and the heads whose prediction fell below its threshold (their
    # values above are the heuristic's and are reported as `unclassified`).
    confidences: Mapping[str, float] = field(default_factory=dict)
    unclassified: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionPlan:
    rlcd_version: str
    model_id: str
    context_strategy: str
    validation_level: str
    confidence: float | None
    reasons: tuple[str, ...]
    fallback_model_ids: tuple[str, ...] = ()


class Router(Protocol):
    """RLCD. May only choose within the policy-eligible set; egress policy re-checks every choice."""

    version: str

    def assess(self, request: RouterRequest) -> InitialAssessment: ...

    def plan(
        self,
        request: RouterRequest,
        assessment: InitialAssessment,
        eligible: list[ModelSpec],
        prices: dict[str, int],
    ) -> ExecutionPlan: ...


class Retriever(Protocol):
    async def knowledge_version(self, tenant_id: str) -> str:
        """Version of the tenant's knowledge base; part of every response-cache key (LLD §11)."""
        ...

    async def retrieve(
        self,
        *,
        query: str,
        tenant_id: str,
        application_id: str,
        profile: ContextProfile | None,
        limit: int,
        min_cosine: float | None = None,
    ) -> list[CimEvidence]:
        """Permitted passages in rank order. `min_cosine` (sovereign-direct retrieval, ADR-0020) keeps only
        passages at least that similar to the query."""
        ...


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    provenance: Provenance = Provenance.ESTIMATED


@dataclass(frozen=True)
class CimCallResult:
    response: CimResponse | None
    usage: TokenUsage
    revision: str
    latency_ms: float
    provenance: Provenance
    error_code: str | None = None
    retryable: bool = False


class ContextIntelligence(Protocol):
    async def package(self, request: CimRequest, model: ModelSpec) -> CimCallResult: ...


@dataclass(frozen=True)
class GuardResult:
    passed: bool
    failures: tuple[str, ...]
    checks: tuple[str, ...]
    # One stable code per failure, in the same order (e.g. `dangling_citation`), for reports that count
    # failures by kind; the messages may be reworded.
    failure_codes: tuple[str, ...] = ()
    # Passages left out whose relevance the Guard did not verify (the CIM's judgement alone, listed as omitted
    # or not mentioned): a package with any is never `verified`, and is not served as it is.
    relevance_unverified: tuple[str, ...] = ()
    # Passages left out that the question-term rule verifies irrelevant (`context_guard.omission_verified`).
    relevance_verified: tuple[str, ...] = ()


class ContextGuard(Protocol):
    version: str

    def verify(
        self, request: CimRequest, response: CimResponse, profile: ContextProfile | None
    ) -> GuardResult: ...


# --- FinOps ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReservationLine:
    """The share of a reservation one model may consume: its model scope and, if local, its GPU pool."""

    model_id: str
    amount_micros: int
    gpu_pool: str | None = None
    gpu_micros: int = 0


@dataclass(frozen=True)
class ReservationRequest:
    run_id: str
    tenant_id: str
    scope_chain: tuple[tuple[str, str], ...]
    amount_micros: int
    idempotency_key: str
    # Money domain the run reserves and commits in (ADR-0008 amendment): `live` for runs that can spend
    # money, otherwise an illustrative domain. See `finops.scopes.run_domain`.
    domain: str
    lines: tuple[ReservationLine, ...] = ()
    # Label of a reservation outside the live domain (`replayed` when the run replays recordings): its hold
    # is illustrative, never presented as measured (ADR-0006). Live reservations are `measured`.
    illustrative_label: Provenance = Provenance.SIMULATED
    # Verified approval this resumed run carries (ADR-0012). A budget approval records the amount it
    # covers; how much an exhausted budget may admit for it is FinOps' decision.
    approval_id: str | None = None


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    amount_micros: int
    scope_chain: tuple[tuple[str, str], ...]
    provenance: Provenance
    lines: tuple[ReservationLine, ...] = ()

    def covers(self, model_id: str) -> bool:
        """Whether a call to `model_id` was reserved for. A reservation without lines covers any model."""
        return not self.lines or any(line.model_id == model_id for line in self.lines)


@dataclass(frozen=True)
class Charge:
    """Actual spend on one model, committed when a reservation is settled. A `tool` charge (a billed tool
    call such as a web search; `model_id` is then the tool ID) counts in the shared scopes only, never in a
    model or GPU-pool budget."""

    model_id: str
    amount_micros: int
    gpu_micros: int = 0
    provenance: Provenance = Provenance.MEASURED
    kind: Literal["model", "tool"] = "model"


class QuotaDeniedError(Exception):
    def __init__(
        self,
        message: str,
        *,
        rule_id: str,
        on_exhaustion: str = "block",
        scope_type: str | None = None,
        scope_id: str | None = None,
        amount_micros: int | None = None,
        resets_at: datetime | None = None,
        already_exhausted: bool = False,
    ) -> None:
        super().__init__(message)
        self.rule_id = rule_id
        self.on_exhaustion = on_exhaustion
        self.scope_type = scope_type
        self.scope_id = scope_id
        # What the denying budget needed for this request: its share of the envelope (API spend, or the GPU
        # estimate for a GPU pool). A budget approval covers this amount (ADR-0014).
        self.amount_micros = amount_micros
        # When the denying budget's period ends and its headroom returns; None for workflow and per-request
        # budgets, which do not reset.
        self.resets_at = resets_at
        # The denying budget already held more than its cap before this request, so an approval for this
        # request's share could never admit it: none is offered (ADR-0014 amendment).
        self.already_exhausted = already_exhausted


class QuotaService(Protocol):
    """Atomic reservations before any billable stage (LLD §12). Unavailable store fails closed."""

    async def reserve(self, request: ReservationRequest) -> Reservation:
        """Raise QuotaDeniedError when the budget is insufficient, DependencyUnavailableError when down."""
        ...

    async def settle(
        self, reservation: Reservation, actual_micros: int, *, charges: tuple[Charge, ...] = ()
    ) -> int:
        """Commit actual spend (per model in `charges`), release the rest. Returns the released amount."""
        ...


# --- Inference and assurance ---------------------------------------------------------------------


@dataclass(frozen=True)
class InferenceRequest:
    run_id: str
    model: ModelSpec
    messages: list[dict[str, str]]
    max_output_tokens: int
    timeout_seconds: float = 120.0
    # Reasoning effort where the provider takes one (Anthropic); None = the adapter's configured effort.
    effort: str | None = None
    # Structured output for this call (OpenAI-compatible adapters); None = the adapter's configured format.
    response_format: dict[str, Any] | None = None
    # Sampling temperature for this call (OpenAI-compatible and Gemini adapters); None = the provider default.
    temperature: float | None = None


@dataclass(frozen=True)
class InferenceResult:
    status: str
    content: str
    finish_reason: str | None
    usage: TokenUsage
    latency_ms: float
    ttft_ms: float | None
    provider_model_version: str
    provenance: Provenance
    error_code: str | None = None
    retryable: bool = False


class InferenceAdapter(Protocol):
    async def complete(self, request: InferenceRequest) -> InferenceResult: ...


@dataclass(frozen=True)
class AnswerContext:
    """What the answering model was given, for the answer-citation check. Computed in-run, never persisted:
    the upstream payload's text and the IDs of the run's candidate evidence (supplied and retrieved)."""

    sent: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class CitationCheck:
    """Assurance check `answer-citations`: `resolved`, `unresolved` or `none` (the answer cites nothing).
    `unresolved_refs` holds identifiers only (strict citation grammar or known evidence IDs), at most 10."""

    status: str
    cited: int
    unresolved: int
    unresolved_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssuranceOutcome:
    passed: bool
    output_dlp: str
    content: str
    checks: tuple[str, ...]
    blocked_reason: str | None = None
    findings: tuple[str, ...] = field(default=())
    # None when no answer context was given (the check did not run).
    citations: CitationCheck | None = None


class AssuranceService(Protocol):
    def check(
        self, content: str, *, output_dlp_required: bool, context: AnswerContext | None = None
    ) -> AssuranceOutcome: ...
