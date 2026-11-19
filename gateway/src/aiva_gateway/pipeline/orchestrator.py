"""LLD §6 request flow. Stage order is explicit; every run emits the same 13 spans (ADR-0004).

Hard rules enforced here, independent of stage implementations:
- policy is evaluated before optimization and again before every CIM, upstream, baseline and tool call;
- a budget reservation exists before any billable stage;
- the sovereign path never falls back to another model;
- RLCD only ever sees the policy-eligible models, and its choice is re-checked at egress.
"""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import Span, Status, StatusCode

from aiva_gateway.auth.principal import RunIdentity
from aiva_gateway.catalog.models import Grounding, ModelSpec
from aiva_gateway.contracts.chat import ChatRequest
from aiva_gateway.contracts.cim import CimEvidence, CimMessage, CimRequest
from aiva_gateway.contracts.common import ErrorBody
from aiva_gateway.contracts.demo import live_demo_unavailable
from aiva_gateway.contracts.policy import PolicyClassification, PolicyDecision, PolicyEgress, PolicyTool
from aiva_gateway.core.canonical import estimate_tokens, sha256_hex
from aiva_gateway.core.enums import (
    AttemptStage,
    ChargeType,
    CimState,
    Classification,
    ExecutionPath,
    Outcome,
    PolicyAction,
    PolicyStage,
    Provenance,
    RunStatus,
    most_restrictive,
)
from aiva_gateway.core.errors import AivaError
from aiva_gateway.core.ids import attempt_id, new_id, run_id
from aiva_gateway.finops.approvals import BUDGET_ALREADY_EXHAUSTED, BUDGET_ALREADY_EXHAUSTED_COPY
from aiva_gateway.finops.envelope import (
    LOWER_COST_ROUTE,
    build_envelope,
    narrow_for_lower_cost,
    upstream_costs,
)
from aiva_gateway.finops.ledger import gpu_estimate_applies, settlement_charges
from aiva_gateway.finops.live import LiveRunWindow, VisitorLimitReached
from aiva_gateway.finops.scopes import (
    LIVE,
    PUBLIC_VISITOR_ROLE,
    illustrative_label,
    run_domain,
    scope_chain,
)
from aiva_gateway.finops.tools import check_tool_call, record_tool_call
from aiva_gateway.govern.policy_input import build_policy_input, tool_arguments_sha256
from aiva_gateway.inference.modes import resolve_execution_mode
from aiva_gateway.inference.registry import AdapterRegistry
from aiva_gateway.inference.tokens import bound_for_estimate, input_token_bound
from aiva_gateway.optimize.cache import build_cache_key
from aiva_gateway.optimize.cim import (
    EVIDENCE_PREAMBLE,
    evidence_block,
    evidence_open_tag,
    passage_counts,
    with_passages,
)
from aiva_gateway.optimize.cim_live import (
    NOTHING_TO_OMIT,
    TASK_OUTSIDE_TRAINING,
    TOO_LARGE_TO_BUILD_IN_TIME,
    cim_bypass_reason,
)
from aiva_gateway.optimize.context_guard import BUDGET_TOLERANCE
from aiva_gateway.optimize.rlcd import grounding, request_costs
from aiva_gateway.optimize.web_search import (
    CREDITS_PER_SEARCH,
    NO_SEARCH_MODES,
    PLANNED_UNITS,
    SEARCH_ATTEMPTS,
    TOOL_ID,
    SearchCall,
    charge_provenance,
    domain,
    evidence_bound,
    passage_token_cap,
    screen,
)
from aiva_gateway.pipeline.context import (
    AttemptRecord,
    BudgetApprovalRecord,
    PolicyEventRecord,
    RunContext,
    WebSearchRecord,
    WebSource,
)
from aiva_gateway.pipeline.interfaces import (
    AnswerContext,
    CachedResponse,
    CimCallResult,
    ContextIntelligence,
    ExecutionPlan,
    InferenceRequest,
    InferenceResult,
    QuotaDeniedError,
    ReservationRequest,
    RouterRequest,
)
from aiva_gateway.services import Services
from aiva_gateway.telemetry.tracing import STAGES, format_trace_id, get_collector, get_tracer

CIM_BYPASS_MESSAGE = "CIM bypassed by mandatory sovereignty policy"
SOVEREIGN_RETRIEVAL_SUPPLIED_MESSAGE = "Knowledge base not searched: the request supplied its evidence"
SOVEREIGN_RETRIEVAL_SUFFICIENT_MESSAGE = (
    "Knowledge base not searched: RLCD predicts the context is sufficient"
)
RETRIEVAL_SKIPPED_TASK_MESSAGE = "Knowledge base not searched: {task} requests do not use it"
MODEL_NOT_ALLOWED_MESSAGE = "Model not allowed by AI policy."
CIM_SKIPPED_MESSAGE = "CIM skipped: context sufficient"
CIM_VERIFIED_MESSAGE = "Context package generated; sufficiency verified"
CIM_FALLBACK_MESSAGE = "Context sufficiency check failed; safe fallback applied"
# A public visitor's live run (ADR-0019) and an internet key call's (ADR-0027) never wake a scaled-to-zero
# endpoint.
ENDPOINT_COLD = "endpoint-cold"
# The policy decision's `matched_network_id` for an application key admitted as an internet caller (ADR-0027).
INTERNET_CALLER_NETWORK = "internet"
CIM_BYPASS_MESSAGES = {
    NOTHING_TO_OMIT: "CIM bypassed: nothing to omit; full context used",
    TOO_LARGE_TO_BUILD_IN_TIME: "CIM bypassed: package too large to build in time; full context used",
    ENDPOINT_COLD: "CIM bypassed: the CIM endpoint is asleep and is not woken; full context used",
    TASK_OUTSIDE_TRAINING: "CIM bypassed: task outside the CIM's trained categories; full context used",
}
CACHE_HIT_MESSAGE = "Full response-cache hit: no upstream inference call"
WEB_SEARCH_DENIED = "Web search blocked by AI policy"
WEB_SEARCH_FAILED = "Web search failed; answered without web evidence"
WEB_SEARCH_EMPTY = "No usable web results; answered without web evidence"
WEB_SEARCH_NOT_RESERVED = "Not searched: the budget reservation was refused"
UPSTREAM_ATTEMPTS = 2  # initial call plus one bounded retry, all reserved up front
CIM_ATTEMPTS = 2  # CIM call plus one bounded retry on timeout or invalid output (LLD §17)
DEFAULT_CIM_BUDGET_TOKENS = 4000
CACHE_TTL_SECONDS = 3600


class _Halt(Exception):
    """Stop the pipeline after `ctx.finish(...)`; remaining stages are emitted as skipped."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class StageRunner:
    """Emits stage spans strictly in STAGES order; stages not run are emitted as skipped."""

    def __init__(self, tracer: trace.Tracer) -> None:
        self._tracer = tracer
        self._next = 0

    def _advance(self, name: str) -> None:
        index = STAGES.index(name)
        if index < self._next:
            raise RuntimeError(f"stage {name} emitted out of order")
        self._next = index + 1

    @contextmanager
    def run(self, name: str) -> Iterator[Span]:
        self._advance(name)
        with self._tracer.start_as_current_span(
            name, record_exception=False, set_status_on_exception=False
        ) as span:
            span.set_attribute("aiva.stage", name)
            span.set_attribute("aiva.stage.status", "executed")
            try:
                yield span
            except _Halt as halt:
                span.set_attribute("aiva.stage.halted", halt.reason)
                raise
            except AivaError as exc:
                span.set_attribute("aiva.error.code", exc.code)
                span.set_status(Status(StatusCode.ERROR, exc.code))
                raise
            except Exception as exc:
                span.set_attribute("aiva.error.code", type(exc).__name__)
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                raise

    def skip(self, name: str, reason: str) -> None:
        self._advance(name)
        with self._tracer.start_as_current_span(name) as span:
            span.set_attribute("aiva.stage", name)
            span.set_attribute("aiva.stage.status", "skipped")
            span.set_attribute("aiva.stage.skip_reason", reason)

    def skip_until_reconciliation(self, reason: str) -> None:
        for name in STAGES[self._next : STAGES.index("reconciliation")]:
            self.skip(name, reason)


def _error(
    code: str,
    message: str,
    *,
    run: str,
    remediation: str | None = None,
    rule_ids: list[str] | None = None,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> ErrorBody:
    return ErrorBody(
        code=code,
        message=message,
        remediation=remediation,
        rule_ids=rule_ids or [],
        retryable=retryable,
        run_id=run,
        details=details or {},
    )


def full_context_messages(
    messages: list[dict[str, str]], evidence: Sequence[CimEvidence]
) -> list[dict[str, str]]:
    """The messages with every passage in one evidence message first: what the answering model is sent when
    nothing is pruned."""
    full = list(messages)
    if evidence:
        blocks = "\n".join(evidence_block(e.id, e.text) for e in evidence)
        full.insert(0, {"role": "system", "content": f"{EVIDENCE_PREAMBLE}\n{blocks}"})
    return full


def full_context_tokens(messages: list[dict[str, str]], evidence: Sequence[CimEvidence]) -> int:
    """The "before" of context savings: the full context's estimated tokens, counted as the constructed
    context is, so a run that sends the full context saves nothing and adds nothing."""
    return sum(estimate_tokens(m["content"]) for m in full_context_messages(messages, evidence))


class Orchestrator:
    def __init__(self, services: Services) -> None:
        self.s = services

    # --- Entry point -------------------------------------------------------------------------------

    def new_context(self, request: ChatRequest, identity: RunIdentity, tenant_id: str) -> RunContext:
        catalog = self.s.catalog
        application = catalog.application(identity.application_id)
        scenario = catalog.scenario(request.scenario_id) if request.scenario_id else None
        profile_id = scenario.context_profile if scenario else None
        department_id = application.department_id if application else None
        messages = [{"role": m.role, "content": m.content} for m in request.messages]
        original_tokens = sum(estimate_tokens(m["content"]) for m in messages) + sum(
            estimate_tokens(e.text) for e in request.evidence
        )
        # The approval reference is excluded so a resumed request hashes like the one that was approved.
        request_hash = sha256_hex(
            {
                "request": request.model_dump(mode="json", exclude={"trace", "approval_id"}),
                "application_id": identity.application_id,
            }
        )
        return RunContext(
            run_id=run_id(),
            tenant_id=tenant_id,
            request=request,
            identity=identity,
            application=application,
            scenario=scenario,
            profile=catalog.context_profile(profile_id) if profile_id else None,
            department_id=department_id,
            cost_center_id=catalog.cost_center_of(department_id),
            business_function=catalog.business_function_of(department_id),
            environment=self.s.settings.environment,
            data_designation=self.s.settings.data_designation,
            request_hash=request_hash,
            messages=messages,
            original_context_tokens=original_tokens,
            context_tokens_before=full_context_tokens(
                messages, [CimEvidence(id=e.id, text=e.text) for e in request.evidence]
            ),
        )

    async def run(self, ctx: RunContext) -> list[ReadableSpan]:
        tracer = get_tracer()
        with tracer.start_as_current_span("aiva.chat") as root:
            ctx.trace_id = format_trace_id(root.get_span_context().trace_id)
            root.set_attribute("aiva.run_id", ctx.run_id)
            stages = StageRunner(tracer)
            try:
                await self._execute(ctx, stages)
            except _Halt as halt:
                stages.skip_until_reconciliation(halt.reason)
            except AivaError as exc:
                ctx.finish(
                    RunStatus.FAILED,
                    Outcome.ERROR,
                    _error(
                        exc.code,
                        exc.message,
                        run=ctx.run_id,
                        remediation=exc.remediation,
                        retryable=exc.retryable,
                    ),
                    http_status=exc.status_code,
                )
                stages.skip_until_reconciliation(f"Stopped: {exc.code}")
            with stages.run("reconciliation") as span:
                await self._reconcile(ctx, span)
            root.set_attribute("aiva.status", ctx.status.value)
            root.set_attribute("aiva.execution_path", ctx.path.value)
        return get_collector().take(root.get_span_context().trace_id)

    # --- Stages ------------------------------------------------------------------------------------

    async def _execute(self, ctx: RunContext, stages: StageRunner) -> None:
        with stages.run("ingress") as span:
            self._ingress(ctx, span)
        with stages.run("policy") as span:
            await self._policy(ctx, span)
        with stages.run("cache") as span:
            await self._cache(ctx, span)
        with stages.run("rlcd1") as span:
            await self._rlcd_initial(ctx, span)

        decision = self._decision(ctx)
        allowed = [m for mid in decision.effective.allowed_models if (m := self.s.catalog.model(mid))]
        assessment = ctx.assessment
        assert assessment is not None
        required = self.s.catalog.model(ctx.required_model) if ctx.required_model else None
        if decision.effective.execution_path == ExecutionPath.SOVEREIGN_DIRECT:
            ctx.path, ctx.cim_state, ctx.cim_message = (
                ExecutionPath.SOVEREIGN_DIRECT,
                CimState.BYPASSED_POLICY,
                CIM_BYPASS_MESSAGE,
            )
            eligible = [m for m in allowed if m.sovereign]
        elif (
            assessment.local_fast_eligible
            and any(not m.external for m in allowed)
            # A required external model is never routed onto the in-house-only local fast path.
            and not (required is not None and required.external)
        ):
            ctx.path, ctx.cim_state, ctx.cim_message = (
                ExecutionPath.LOCAL_FAST,
                CimState.SKIPPED_SUFFICIENT,
                CIM_SKIPPED_MESSAGE,
            )
            eligible = [m for m in allowed if not m.external]
        else:
            ctx.path = ExecutionPath.CIM_ASSISTED
            eligible = allowed
        if ctx.required_model is not None:
            eligible = self._require_model(ctx, eligible)
        await self._check_planned_path(ctx, eligible)

        if ctx.path is ExecutionPath.CIM_ASSISTED:
            skip_retrieval = self._retrieval_skip(ctx)
            if skip_retrieval is None:
                with stages.run("retrieval") as span:
                    eligible = await self._retrieval(ctx, span, eligible)
            else:
                ctx.evidence = list(self._request_evidence(ctx))
                stages.skip("retrieval", skip_retrieval)
            with stages.run("cim") as span:
                eligible = await self._cim(ctx, span, eligible)
            if ctx.cim_result and ctx.cim_result.response:
                with stages.run("context_guard") as span:
                    self._context_guard(ctx, span)
            else:
                stages.skip("context_guard", "No context package to verify; full eligible context used")
        else:
            ctx.evidence = list(self._request_evidence(ctx))
            skip_retrieval = self._sovereign_retrieval_skip(ctx)
            if skip_retrieval is None:
                with stages.run("retrieval") as span:
                    await self._sovereign_retrieval(ctx, span)
            else:
                stages.skip("retrieval", skip_retrieval)
            for name in ("cim", "context_guard"):
                stages.skip(name, ctx.cim_message)
            self._use_full_context(ctx)

        with stages.run("rlcd2") as span:
            self._rlcd_final(ctx, span, eligible)
            await self._never_wake(ctx, span)
        with stages.run("quota") as span:
            if ctx.reservation is None:
                await self._reserve(ctx, span, eligible, include_cim=False)
                ctx.reserved_at = "quota"
            else:
                span.set_attribute("aiva.quota.envelope_reserved_at", ctx.reserved_at or "cim")
        with stages.run("preflight") as span:
            await self._preflight(ctx, span)
        with stages.run("inference") as span:
            await self._inference(ctx, span)
        with stages.run("assurance") as span:
            self._assurance(ctx, span)

    def _ingress(self, ctx: RunContext, span: Span) -> None:
        ctx.public_visitor = ctx.identity.is_public_visitor
        mode = resolve_execution_mode(
            ctx.request.execution_mode, configured=self.s.settings.inference_mode, identity=ctx.identity
        )
        ctx.execution_mode, ctx.per_run_replay = mode.mode, mode.per_run_replay
        span.set_attribute("aiva.execution_mode", ctx.execution_mode)
        span.set_attribute("aiva.execution_mode.per_run_replay", ctx.per_run_replay)
        span.set_attribute("aiva.application_id", ctx.identity.application_id)
        span.set_attribute("aiva.identity_mode", ctx.identity.identity_mode.value)
        span.set_attribute("aiva.source_mode", ctx.identity.source_mode.value)
        span.set_attribute("aiva.message_count", len(ctx.messages))
        span.set_attribute("aiva.evidence_count", len(ctx.request.evidence))
        span.set_attribute("aiva.original_context_tokens", ctx.original_context_tokens)
        if ctx.scenario:
            span.set_attribute("aiva.scenario_id", ctx.scenario.id)

    def classify(self, ctx: RunContext) -> None:
        """DLP-scan messages and evidence; effective classification = most restrictive signal (LLD §7)."""
        dlp = self.s.dlp
        ctx.dlp = dlp.scan("\n".join(m["content"] for m in ctx.messages))
        # Evidence is untrusted data: injection text inside it is recorded but never obeyed or executed.
        ctx.evidence_dlp = dlp.scan("\n".join(e.text for e in ctx.request.evidence))
        floor = ctx.application.classification_floor if ctx.application else None
        signals = [c for c in (ctx.dlp.classification, ctx.evidence_dlp.classification) if c is not None]
        detected = most_restrictive(*signals) if signals else None
        declared = ctx.request.data_classification
        ctx.classification = PolicyClassification(
            declared=declared,
            floor=floor,
            dlp=detected,
            rlcd=None,
            effective=most_restrictive(declared, floor, detected),
        )

    async def dry_run_policy(self, ctx: RunContext) -> PolicyDecision:
        """Request-stage decision without executing or recording a run (`POST /v1/policy/evaluate`)."""
        self.classify(ctx)
        return await self._evaluate(ctx, PolicyStage.REQUEST)

    async def _policy(self, ctx: RunContext, span: Span) -> None:
        self.classify(ctx)
        assert ctx.dlp is not None and ctx.evidence_dlp is not None
        decision = await self._evaluate(ctx, PolicyStage.REQUEST)
        span.set_attribute("aiva.dlp.finding_types", ctx.dlp.finding_types + ctx.evidence_dlp.finding_types)
        span.set_attribute("aiva.dlp.scanner_version", ctx.dlp.scanner_version)
        span.set_attribute("aiva.dlp.injection_detected", ctx.dlp.injection_detected)
        span.set_attribute(
            "aiva.dlp.evidence_injection_indicators", list(ctx.evidence_dlp.injection_indicators)
        )
        self._apply_request_decision(ctx, decision)

    def _apply_request_decision(self, ctx: RunContext, decision: PolicyDecision) -> None:
        ctx.request_decision = decision
        rule_ids = [r.rule_id for r in decision.reasons]
        if decision.action == PolicyAction.DENY:
            first = decision.reasons[0] if decision.reasons else None
            ctx.finish(
                RunStatus.BLOCKED,
                Outcome.POLICY_DENIED,
                _error(
                    "POLICY_DENIED",
                    "Blocked by AI policy.",
                    run=ctx.run_id,
                    rule_ids=rule_ids,
                    remediation=first.remediation if first else None,
                ),
            )
            raise _Halt("Blocked by AI policy")
        if decision.action == PolicyAction.APPROVAL_REQUIRED:
            ctx.approval_id = ctx.policy_events[-1].approval_id
            ctx.finish(
                RunStatus.APPROVAL_REQUIRED,
                Outcome.APPROVAL_REQUIRED,
                _error(
                    "APPROVAL_REQUIRED",
                    "Approval required.",
                    run=ctx.run_id,
                    rule_ids=rule_ids,
                    remediation="An approver must review this request in the AiVa Govern approval queue.",
                ),
            )
            raise _Halt("Approval required")

    async def _cache(self, ctx: RunContext, span: Span) -> None:
        decision = self._decision(ctx)
        knowledge_version = await self.s.retriever.knowledge_version(ctx.tenant_id)
        key, scope_rule = build_cache_key(ctx, decision, knowledge_version=knowledge_version)
        span.set_attribute("aiva.cache.type", self.s.cache.cache_type)
        span.set_attribute("aiva.cache.scope_rule", scope_rule)
        if key is None:
            span.set_attribute("aiva.cache.hit", False)
            return
        ctx.cache_checked, ctx.cache_key_hash, ctx.cache_scope = True, key.key_hash, key.scope
        ctx.cache_key = key
        assert ctx.classification is not None
        ctx.cache_classification = ctx.classification.effective
        ctx.cache_type = (
            "exact+semantic" if key.semantic and "semantic" in self.s.cache.cache_type else "exact"
        )
        span.set_attribute("aiva.cache.semantic_eligible", key.semantic is not None)
        hit = await self.s.cache.lookup(key)
        span.set_attribute("aiva.cache.hit", hit is not None)
        if hit is None:
            return
        span.set_attribute("aiva.cache.hit_type", hit.cache_type)
        if hit.similarity is not None:
            span.set_attribute("aiva.cache.similarity", hit.similarity)
        ctx.path, ctx.cim_state, ctx.cim_message = (
            ExecutionPath.CACHE_RETURN,
            CimState.SKIPPED_CACHE,
            CACHE_HIT_MESSAGE,
        )
        ctx.cache_hit_entry, ctx.cache_type = hit, hit.cache_type
        # The cached answer keeps the provenance of the run that produced it (never upgraded to measured).
        ctx.response_content, ctx.response_provenance = hit.content, hit.provenance
        ctx.selected_model = self.s.catalog.model(hit.model_id)
        ctx.finish(RunStatus.COMPLETED, Outcome.SUCCESS)
        raise _Halt(CACHE_HIT_MESSAGE)

    def _router_request(self, ctx: RunContext) -> RouterRequest:
        return RouterRequest(
            task=ctx.request.task,
            scenario_task=ctx.scenario.task_category if ctx.scenario else None,
            quality_tier=ctx.request.quality_tier,
            original_context_tokens=ctx.original_context_tokens,
            evidence_count=len(ctx.request.evidence),
            preferred_model=ctx.request.routing.preferred_model,
            routing_mode=ctx.request.routing.mode,
            prediction=ctx.rlcd_prediction,
            grounding=self._grounding(ctx),
        )

    async def _rlcd_initial(self, ctx: RunContext, span: Span) -> None:
        # The classifier runs off the event loop under its own time budget; None means the heuristic serves.
        ctx.rlcd_prediction = await self.s.rlcd.classify(ctx.messages)
        if ctx.rlcd_prediction is not None:
            span.set_attribute("aiva.rlcd.model_latency_ms", round(ctx.rlcd_prediction.latency_ms, 1))
        assessment = self.s.router.assess(self._router_request(ctx))
        ctx.assessment = assessment
        span.set_attribute("aiva.rlcd.version", assessment.rlcd_version)
        if assessment.unclassified:
            span.set_attribute("aiva.rlcd.unclassified", ",".join(assessment.unclassified))
        span.set_attribute("aiva.rlcd.task_category", assessment.task_category)
        span.set_attribute("aiva.rlcd.complexity", assessment.complexity)
        span.set_attribute("aiva.rlcd.local_fast_eligible", assessment.local_fast_eligible)
        predicted = assessment.predicted_sensitivity
        assert ctx.classification is not None
        if predicted is not None and predicted.rank > ctx.classification.effective.rank:
            # RLCD may only tighten classification (ADR-0005); re-evaluate the request policy.
            ctx.classification = replace_classification(ctx.classification, rlcd=predicted)
            span.set_attribute("aiva.rlcd.tightened_to", predicted.value)
            self._apply_request_decision(ctx, await self._evaluate(ctx, PolicyStage.REQUEST))

    async def _retrieval(self, ctx: RunContext, span: Span, eligible: list[ModelSpec]) -> list[ModelSpec]:
        query = next((m["content"] for m in reversed(ctx.messages) if m["role"] == "user"), "")
        retrieved = await self.s.retriever.retrieve(
            query=query,
            tenant_id=ctx.tenant_id,
            application_id=ctx.identity.application_id,
            profile=ctx.profile,
            limit=8,
        )
        # One id, one passage: a KB chunk whose id the caller already supplied is not added, so the caller's
        # passage keeps the id its run and citations use (supplied ids are unique: `EvidenceList`).
        supplied = {e.id for e in ctx.request.evidence}
        ctx.retrieved = [r for r in retrieved if r.id not in supplied]
        ctx.evidence = [*self._request_evidence(ctx), *ctx.retrieved]
        ctx.passages_retrieved = len(ctx.evidence)
        ctx.context_tokens_before = full_context_tokens(ctx.messages, ctx.evidence)
        span.set_attribute("aiva.retrieval.supplied", len(ctx.request.evidence))
        span.set_attribute("aiva.retrieval.retrieved", len(ctx.retrieved))
        if len(ctx.retrieved) < len(retrieved):
            span.set_attribute("aiva.retrieval.id_collisions", len(retrieved) - len(ctx.retrieved))
        if ctx.profile is not None and ctx.profile.web_search_k:
            eligible = await self._web_search(ctx, span, eligible, query)
        return eligible

    async def _web_search(
        self, ctx: RunContext, span: Span, eligible: list[ModelSpec], query: str
    ) -> list[ModelSpec]:
        """Governed live web search (ADR-0022), after the knowledge base. The tool must be available to this
        run; policy must allow the call (evaluated again before the retry); the envelope is reserved before
        the first call, covering every attempt and the passages the results can add. Only the question is
        sent. Screened results join the evidence. Unavailable, denied or failed, the run answers without web
        evidence and says so. Spans get counts and domains only."""
        assert ctx.profile is not None
        k = ctx.profile.web_search_k
        record = ctx.web_search = WebSearchRecord(provider=self.s.search.provider, status="unavailable")
        record.detail = NO_SEARCH_MODES.get(ctx.execution_mode) or self.s.search.reason
        try:
            if record.detail is None:
                eligible = await self._governed_search(ctx, span, eligible, record, query, k)
        finally:
            span.set_attribute("aiva.search.status", record.status)
            span.set_attribute("aiva.search.provider", record.provider)
            span.set_attribute("aiva.search.attempts", record.attempts)
            span.set_attribute("aiva.search.results_returned", record.results_returned)
            span.set_attribute("aiva.search.results_used", record.results_used)
            span.set_attribute("aiva.search.dropped_injection", record.dropped_injection)
            span.set_attribute("aiva.search.dropped_dlp", record.dropped_dlp)
            span.set_attribute("aiva.search.domains", record.domains)
            span.set_attribute("aiva.search.billed_units", sum(c.units for c in ctx.tool_charges))
            if record.rule_ids:
                span.set_attribute("aiva.search.rule_ids", record.rule_ids)
            if record.error_code:
                span.set_attribute("aiva.search.error_code", record.error_code)
        return eligible

    async def _governed_search(
        self,
        ctx: RunContext,
        span: Span,
        eligible: list[ModelSpec],
        record: WebSearchRecord,
        query: str,
        k: int,
    ) -> list[ModelSpec]:
        decision = await self.authorize_tool_call(ctx, TOOL_ID)
        if decision.action != PolicyAction.ALLOW:
            record.status, record.detail = "denied", WEB_SEARCH_DENIED
            record.rule_ids = [r.rule_id for r in decision.reasons]
            return eligible
        # A refused reservation halts the run here; the record says why nothing was searched.
        record.status, record.detail, record.error_code = "failed", WEB_SEARCH_NOT_RESERVED, "NOT_RESERVED"
        ctx.planned_tool_units[TOOL_ID] = PLANNED_UNITS
        ctx.planned_evidence_tokens = evidence_bound(k)
        eligible = await self._reserve(ctx, span, eligible, include_cim=True)
        ctx.reserved_at = "retrieval"
        call = await self._search(ctx, record, query, k)
        record.results_returned, record.error_code = len(call.results), call.error_code
        if call.status != "ok":
            record.status, record.detail = "failed", WEB_SEARCH_FAILED
            return eligible
        screened = screen(
            call.results,
            self.s.dlp,
            taken_ids={e.id for e in ctx.evidence},
            max_tokens=passage_token_cap(ctx.profile.target_budget_tokens, k) if ctx.profile else None,
        )
        record.status = "searched"
        record.detail = None if screened.passages else WEB_SEARCH_EMPTY
        record.results_used = len(screened.passages)
        record.dropped_injection = screened.dropped_injection
        record.dropped_dlp = screened.dropped_dlp
        record.sources = [
            WebSource(evidence.id, title, url)
            for evidence, (title, url) in zip(screened.passages, screened.sources, strict=True)
        ]
        record.domains = list(dict.fromkeys(domain(url) for _, url in screened.sources))
        if screened.passages:
            # In this process's memory only, briefly: a presenter's comparison gives the frontier reference
            # the same web evidence (ADR-0023). Never in records, spans or logs.
            self.s.web_evidence.keep(ctx.run_id, screened.passages)
        ctx.evidence = [*ctx.evidence, *screened.passages]
        ctx.passages_retrieved = len(ctx.evidence)
        ctx.context_tokens_before = full_context_tokens(ctx.messages, ctx.evidence)
        return eligible

    async def _search(self, ctx: RunContext, record: WebSearchRecord, query: str, k: int) -> SearchCall:
        """One search with one bounded retry. Each call is policy-checked, admitted only within what the
        reservation holds for it, and metered with the units the provider billed or may have billed."""
        call = SearchCall("failed", error_code="SEARCH_NOT_CALLED")
        for attempt in range(SEARCH_ATTEMPTS):
            if attempt and (await self.authorize_tool_call(ctx, TOOL_ID)).action != PolicyAction.ALLOW:
                break
            try:
                check_tool_call(ctx, self.s.prices, TOOL_ID, CREDITS_PER_SEARCH)
            except AivaError as exc:
                call = SearchCall("failed", error_code=exc.code)
                break
            call = await self.s.search.search(query, max_results=k)
            record.attempts += 1
            if call.billed_units is not None:
                provenance = charge_provenance(ctx.execution_mode, call)
                record_tool_call(ctx, self.s.prices, TOOL_ID, call.billed_units, provenance)
            if call.status == "ok" or not call.retryable:
                break
        return call

    def _web_passages(self, ctx: RunContext) -> list[CimEvidence]:
        ids = {source.evidence_id for source in ctx.web_search.sources} if ctx.web_search else set()
        return [e for e in ctx.evidence if e.id in ids]

    def _mark_web_sent(self, ctx: RunContext, span: Span) -> None:
        """Which web passages the final payload carries to the answering model, so the response and the
        audit never count a passage that passed screening but was not sent."""
        if ctx.web_search is None or not ctx.web_search.sources:
            return
        payload = "\n".join(m["content"] for m in ctx.upstream_messages)
        for source in ctx.web_search.sources:
            source.sent = evidence_open_tag(source.evidence_id) in payload
        span.set_attribute("aiva.search.results_sent", ctx.web_search.results_sent)

    def _require_model(self, ctx: RunContext, eligible: list[ModelSpec]) -> list[ModelSpec]:
        """The required model alone, when policy and the execution path leave it eligible (ADR-0026); else
        the run is blocked here, before any billable stage. It is never substituted: RLCD then chooses inside
        a set of one, so it cannot widen what the caller asked for or what policy allows."""
        kept = [m for m in eligible if m.id == ctx.required_model]
        if kept:
            return kept
        ctx.finish(
            RunStatus.BLOCKED,
            Outcome.POLICY_DENIED,
            _error(
                "MODEL_NOT_ALLOWED",
                MODEL_NOT_ALLOWED_MESSAGE,
                run=ctx.run_id,
                remediation="Use a model GET /v1/models lists, or `aiva-auto` to let AiVa choose.",
            ),
            http_status=403,
        )
        raise _Halt("Model not allowed by AI policy")

    def _retrieval_skip(self, ctx: RunContext) -> str | None:
        """Why the CIM-assisted path searches neither the knowledge base nor the web, or None to search: the
        run's task category (declared, or RLCD's) is one `context_profiles.yaml` excludes. The request keeps
        the evidence it supplied."""
        task = ctx.assessment.task_category if ctx.assessment else None
        if task is not None and task in self.s.catalog.retrieval_skip_tasks:
            return RETRIEVAL_SKIPPED_TASK_MESSAGE.format(task=task)
        return None

    def _sovereign_retrieval_skip(self, ctx: RunContext) -> str | None:
        """Why the knowledge base is not searched on a path without the CIM, or None to search it (ADR-0020):
        only on the sovereign-direct path, for a context profile that opts in, when the caller supplied no
        evidence and RLCD does not confidently predict the context is sufficient (with the heuristic serving,
        the profile decides)."""
        profile, assessment = ctx.profile, ctx.assessment
        if (
            ctx.path is not ExecutionPath.SOVEREIGN_DIRECT
            or profile is None
            or not profile.sovereign_retrieval_k
        ):
            return ctx.cim_message
        if ctx.request.evidence:
            return SOVEREIGN_RETRIEVAL_SUPPLIED_MESSAGE
        if (
            assessment is not None
            and assessment.context_sufficient
            and "context_need" in assessment.confidences
            and "context_need" not in assessment.unclassified
        ):
            return SOVEREIGN_RETRIEVAL_SUFFICIENT_MESSAGE
        return None

    async def _sovereign_retrieval(self, ctx: RunContext, span: Span) -> None:
        """Knowledge-base passages for the sovereign model (ADR-0020). In-process: the query, the ranking and
        the passages never leave the gateway, and they reach only the sovereign model, never the CIM, the
        baseline or an external model. Only passages at least as similar to the question as the profile's
        floor, at most k, within the profile's token budget: with no CIM to prune them, they join the full
        context as they are. Retrieval runs before the reservation, so the envelope covers them."""
        profile = ctx.profile
        assert profile is not None
        query = next((m["content"] for m in reversed(ctx.messages) if m["role"] == "user"), "")
        retrieved = await self.s.retriever.retrieve(
            query=query,
            tenant_id=ctx.tenant_id,
            application_id=ctx.identity.application_id,
            profile=profile,
            limit=profile.sovereign_retrieval_k,
            min_cosine=profile.sovereign_retrieval_min_cosine,
        )
        kept: list[CimEvidence] = []
        tokens = 0
        for passage in retrieved:
            tokens += estimate_tokens(evidence_block(passage.id, passage.text))
            if tokens > profile.target_budget_tokens:
                break
            kept.append(passage)
        ctx.retrieved = kept
        ctx.evidence = [*ctx.evidence, *kept]
        ctx.passages_retrieved = len(ctx.evidence)
        ctx.context_tokens_before = full_context_tokens(ctx.messages, ctx.evidence)
        span.set_attribute("aiva.retrieval.path", ExecutionPath.SOVEREIGN_DIRECT.value)
        span.set_attribute("aiva.retrieval.supplied", 0)
        span.set_attribute("aiva.retrieval.retrieved", len(kept))
        if len(kept) < len(retrieved):
            span.set_attribute("aiva.retrieval.over_budget", len(retrieved) - len(kept))

    async def _cim(self, ctx: RunContext, span: Span, eligible: list[ModelSpec]) -> list[ModelSpec]:
        # Recall floor: the top-ranked retrieved passages are kept whatever the CIM selects, and so is every
        # web passage that passed screening (ADR-0022): the CIM was not trained on web results, and a search
        # the answer never saw must not be shown as its evidence. They fit the budget (`passage_token_cap`).
        kb_floor = ctx.retrieved[: ctx.profile.keep_top_k] if ctx.profile else []
        web_floor = self._web_passages(ctx)
        floor = [*kb_floor, *web_floor]
        cim_spec = self.s.catalog.model(self.s.catalog.models.cim_model)
        contract = cim_spec.cim_contract if cim_spec else "v1"
        settings = self.s.settings
        bypass = cim_bypass_reason(
            ctx.evidence,
            ctx.messages,
            ctx.profile,
            decode_tokens_per_s=(
                settings.cim_v2_decode_tokens_per_s if contract == "v2" else settings.cim_decode_tokens_per_s
            ),
            floor=floor,
            contract=contract,
            task=ctx.assessment.task_category if ctx.assessment else None,
            trained_tasks=cim_spec.cim_trained_tasks if cim_spec else None,
        )
        if bypass is None and cim_spec is not None and not await self._awake(ctx, cim_spec):
            bypass = ENDPOINT_COLD
        if bypass is not None:
            # The Guard could only verify the full context, or the passages it requires could not be emitted
            # within the CIM's time budget. No call, no CIM reservation; the full context is used.
            ctx.cim_state, ctx.cim_message = CimState.SKIPPED_SUFFICIENT, CIM_BYPASS_MESSAGES[bypass]
            span.set_attribute("aiva.cim.bypass_reason", bypass)
            self._use_full_context(ctx)
            return eligible
        # The workflow envelope is reserved before the first billable stage: the CIM call, unless a web
        # search already reserved it.
        if ctx.reservation is None:
            eligible = await self._reserve(ctx, span, eligible, include_cim=True)
            ctx.reserved_at = "cim"
        cim_model = self.s.catalog.model(self.s.catalog.models.cim_model)
        assert cim_model is not None
        if ctx.reservation is None or not ctx.reservation.covers(cim_model.id):
            raise AivaError(
                "The CIM call is not covered by the budget reservation.", code="RESERVATION_INSUFFICIENT"
            )
        egress = await self._evaluate(
            ctx, PolicyStage.EGRESS, PolicyEgress(model_id=cim_model.id, stage=AttemptStage.CIM)
        )
        if egress.action != PolicyAction.ALLOW:
            ctx.cim_state, ctx.cim_message = (
                CimState.BYPASSED_POLICY,
                "CIM not permitted by policy; full context used",
            )
            span.set_attribute("aiva.cim.egress", "denied")
            self._use_full_context(ctx)
            return eligible
        profile = ctx.profile
        assert ctx.assessment is not None
        ctx.cim_request = CimRequest(
            task=ctx.assessment.task_category,
            messages=[CimMessage.model_validate(m) for m in ctx.messages],
            evidence=ctx.evidence,
            context_profile=profile.id if profile else "general",
            target_budget_tokens=profile.target_budget_tokens if profile else DEFAULT_CIM_BUDGET_TOKENS,
            required_fields=profile.required_fields if profile else [],
        )
        result = await self._package(ctx, cim_model, ctx.cim_request)
        if result.response is not None and floor:
            package = with_passages(result.response, floor)
            ctx.recall_floor_ids = [
                i for i in package.selected_evidence_ids if i not in result.response.selected_evidence_ids
            ]
            result = replace(result, response=package)
            web_ids = {p.id for p in web_floor}
            web_added = sum(i in web_ids for i in ctx.recall_floor_ids)
            span.set_attribute("aiva.cim.recall_floor_added", len(ctx.recall_floor_ids) - web_added)
            if web_floor:
                span.set_attribute("aiva.cim.web_floor_added", web_added)
        ctx.cim_result = result
        ctx.cim_state = CimState.INVOKED
        if result.response is not None:
            counts = passage_counts([e.id for e in ctx.evidence], result.response.selected_evidence_ids)
            span.set_attribute("aiva.cim.passages_kept", counts.kept)
            span.set_attribute("aiva.cim.passages_omitted", counts.omitted)
        span.set_attribute("aiva.cim.revision", result.revision)
        span.set_attribute("aiva.cim.provenance", result.provenance.value)
        if result.provenance is not Provenance.SIMULATED:
            # v2 (ADR-0018): the CIM returned a selection and the gateway built the package; v1: the CIM's.
            span.set_attribute("aiva.cim.contract", cim_model.cim_contract)
        span.set_attribute("aiva.cim.attempts", sum(a.stage is AttemptStage.CIM for a in ctx.attempts))
        if result.error_code:
            span.set_attribute("aiva.cim.error_code", result.error_code)
        if result.response is None:
            ctx.cim_message = CIM_FALLBACK_MESSAGE
            ctx.sufficiency = "failed-fallback-applied"
            self._use_full_context(ctx)
        return eligible

    async def _package(self, ctx: RunContext, cim_model: ModelSpec, request: CimRequest) -> CimCallResult:
        """CIM call with one bounded retry; every attempt is policy-checked at egress and metered."""
        retry_of: str | None = None
        result: CimCallResult | None = None
        for attempt in range(CIM_ATTEMPTS):
            if attempt:
                egress = await self._evaluate(
                    ctx, PolicyStage.EGRESS, PolicyEgress(model_id=cim_model.id, stage=AttemptStage.CIM)
                )
                if egress.action != PolicyAction.ALLOW:
                    break
            started = datetime.now(UTC)
            result = await self._cim_client(ctx).package(request, cim_model)
            record = self._record_attempt(
                ctx,
                AttemptStage.CIM,
                cim_model,
                started,
                InferenceResult(
                    status="ok" if result.response else "error",
                    content="",
                    finish_reason=None,
                    usage=result.usage,
                    latency_ms=result.latency_ms,
                    ttft_ms=None,
                    provider_model_version=result.revision,
                    provenance=result.provenance,
                    error_code=result.error_code,
                ),
                ChargeType.RETRY if retry_of else ChargeType.CIM,
                retry_of=retry_of,
            )
            if result.response is not None or not result.retryable:
                break
            retry_of = record.attempt_id
        assert result is not None
        return result

    def _context_guard(self, ctx: RunContext, span: Span) -> None:
        assert (
            ctx.cim_request is not None and ctx.cim_result is not None and ctx.cim_result.response is not None
        )
        request, package = ctx.cim_request, ctx.cim_result.response
        guard = self.s.context_guard.verify(request, package, ctx.profile)
        # Put back every left-out passage the question-term rule cannot verify and drop only those it does
        # (ADR-0009, option A). Verify again: a passage put back can make another one its sibling. With
        # nothing verified left, putting everything back is the full context: fall back to it instead. A
        # package with passages put back is bounded by the full context, which the fallback would send, not
        # by the budget the CIM was given.
        put_back: list[str] = []
        bound = request.model_copy(
            update={"target_budget_tokens": max(request.target_budget_tokens, ctx.context_tokens_before)}
        )
        while guard.passed and guard.relevance_unverified and guard.relevance_verified:
            back = [e for e in request.evidence if e.id in set(guard.relevance_unverified)]
            package = with_passages(package, back)
            put_back += [e.id for e in back]
            guard = self.s.context_guard.verify(bound, package, ctx.profile)
        if put_back and guard.passed and not guard.relevance_unverified:
            ctx.cim_result = replace(ctx.cim_result, response=package)
            ctx.relevance_put_back = put_back
        ctx.guard = guard
        span.set_attribute("aiva.guard.version", self.s.context_guard.version)
        span.set_attribute("aiva.guard.passed", guard.passed)
        span.set_attribute("aiva.guard.failure_count", len(guard.failures))
        span.set_attribute("aiva.guard.relevance_unverified", len(guard.relevance_unverified))
        span.set_attribute("aiva.guard.relevance_verified", len(guard.relevance_verified))
        span.set_attribute("aiva.guard.relevance_put_back", len(ctx.relevance_put_back))
        if guard.passed and not guard.relevance_unverified:
            ctx.upstream_messages = [
                {"role": m.role, "content": m.content} for m in package.upstream_messages
            ]
            ctx.constructed_context_tokens = sum(estimate_tokens(m["content"]) for m in ctx.upstream_messages)
            ctx.sufficiency, ctx.cim_message, ctx.context_strategy = (
                "verified",
                CIM_VERIFIED_MESSAGE,
                "evidence-preserving",
            )
        else:
            # Never pass an unchecked package: restore the full eligible context instead (LLD §17). A package
            # that omits passages on the CIM's judgement alone (`relevance_unverified`) is not verified
            # either, so it is never served (ADR-0009, 2026-10-22 amendment): the full context keeps every
            # passage the CIM cannot rule out.
            ctx.sufficiency, ctx.cim_message = "failed-fallback-applied", CIM_FALLBACK_MESSAGE
            self._use_full_context(ctx)

    def _rlcd_final(self, ctx: RunContext, span: Span, eligible: list[ModelSpec]) -> None:
        if not eligible:
            ctx.finish(
                RunStatus.BLOCKED,
                Outcome.POLICY_DENIED,
                _error("NO_ELIGIBLE_MODEL", "No model is eligible on this execution path.", run=ctx.run_id),
            )
            raise _Halt("No eligible model")
        plan = self._plan(ctx, eligible)
        notes = [*ctx.budget_notes]
        if ctx.required_model is not None:
            notes.append(f"model {ctx.required_model} required by the caller, inside the policy-eligible set")
        ctx.plan = replace(plan, reasons=(*plan.reasons, *notes)) if notes else plan
        ctx.selected_model = self.s.catalog.model(plan.model_id)
        span.set_attribute("aiva.rlcd.version", plan.rlcd_version)
        span.set_attribute("aiva.rlcd.model_id", plan.model_id)
        span.set_attribute("aiva.rlcd.eligible", [m.id for m in eligible])
        span.set_attribute("aiva.rlcd.grounding", self._grounding(ctx))
        span.set_attribute("aiva.rlcd.reasons", list(plan.reasons))

    async def _reserve(
        self, ctx: RunContext, span: Span, eligible: list[ModelSpec], *, include_cim: bool
    ) -> list[ModelSpec]:
        """Reserve the worst-case envelope before the first billable stage (LLD §6 step 10, §12).

        Before RLCD's final plan (CIM path) the envelope covers every eligible model; after it, the chosen
        one. Returns the candidates that fit: narrowed only by a `lower_cost_route` budget, and only ever
        within the policy-eligible set, so saving money never crosses a confidentiality boundary.
        """
        request = ctx.request
        # Upper bound on billed input for any upstream payload (provider tokenizers count more than the
        # 4-chars-per-token estimate): the full eligible context, or a package within the CIM token budget
        # the Context Guard enforces, whichever is larger. Preflight re-checks the real payload against it.
        # A web search reserves before it runs: its passages are bounded, not yet known.
        full_context = full_context_messages(ctx.messages, ctx.evidence)
        input_tokens = input_token_bound(full_context) + ctx.planned_evidence_tokens
        if include_cim:
            target = ctx.profile.target_budget_tokens if ctx.profile else DEFAULT_CIM_BUDGET_TOKENS
            package = bound_for_estimate(int(target * BUDGET_TOLERANCE), messages=len(ctx.messages) + 1)
            input_tokens = max(input_tokens, package)
        ctx.reserved_input_tokens = input_tokens
        catalog = self.s.catalog
        cim_model = catalog.model(catalog.models.cim_model) if include_cim else None
        frontier = catalog.model(catalog.models.frontier_baseline)
        baseline = None
        if (
            request.baseline_comparison
            and frontier
            and frontier.id in self._decision(ctx).effective.allowed_models
            # Served by the frontier model itself: no comparison to run, so nothing to hold for it.
            and (include_cim or ctx.selected_model is None or ctx.selected_model.id != frontier.id)
        ):
            baseline = frontier
            # One number for the baseline's reservation line and its call (`_call`).
            floor = catalog.models.baseline.max_output_tokens or 0
            ctx.baseline_max_output_tokens = max(request.max_output_tokens, floor)
        sizing = {
            "upstream_attempts": UPSTREAM_ATTEMPTS,
            "input_tokens": input_tokens,
            "max_output_tokens": request.max_output_tokens,
        }
        costs = upstream_costs(self.s.prices, catalog.models.models, **sizing)
        domain = self._domain(ctx)
        public_live = self._public_live(ctx)
        chain = scope_chain(
            tenant_id=ctx.tenant_id,
            department_id=ctx.department_id,
            application_id=ctx.identity.application_id,
            principal_pseudonym=ctx.identity.principal_pseudonym,
            workflow_id=request.workflow_id,
            public_demo=public_live,
            internet_gpu=self._internet_live(ctx),
        )
        if public_live:
            await self._take_live_slot(ctx)
        pool = list(eligible)
        while True:
            if include_cim:
                candidates = pool
            else:
                assert ctx.selected_model is not None
                candidates = [ctx.selected_model]
            envelope = build_envelope(
                self.s.prices,
                upstream=candidates,
                cim=cim_model,
                cim_attempts=CIM_ATTEMPTS,
                baseline=baseline,
                baseline_max_output_tokens=ctx.baseline_max_output_tokens or None,
                tools=ctx.planned_tool_units,
                **sizing,
            )
            try:
                ctx.reservation = await self.s.quota.reserve(
                    ReservationRequest(
                        run_id=ctx.run_id,
                        tenant_id=ctx.tenant_id,
                        scope_chain=chain,
                        amount_micros=envelope.amount_micros,
                        idempotency_key=ctx.run_id,
                        domain=domain,
                        # The run's mode (ADR-0019): a per-run replay hold is `replayed`.
                        illustrative_label=illustrative_label(ctx.execution_mode),
                        lines=envelope.lines,
                        approval_id=ctx.approval.approval_id if ctx.approval else None,
                    )
                )
                # A billed tool call is admitted only within what this reservation holds (finops.tools).
                ctx.reserved_tool_micros = envelope.tool_micros
                break
            except QuotaDeniedError as exc:
                narrowed = []
                if exc.on_exhaustion == LOWER_COST_ROUTE:
                    narrowed = narrow_for_lower_cost(pool, candidates, exc, costs, self.s.prices)
                if not narrowed:
                    self._quota_denied(ctx, exc, envelope.amount_micros)
                excluded = sorted({m.id for m in pool} - {m.id for m in narrowed})
                ctx.budget_notes.append(
                    f"lower-cost route: budget {exc.rule_id} excluded {', '.join(excluded)}"
                )
                pool = narrowed
                if not include_cim:
                    self._replan(ctx, pool)
        span.set_attribute("aiva.quota.reservation_id", ctx.reservation.reservation_id)
        span.set_attribute("aiva.quota.reserved_micros", ctx.reservation.amount_micros)
        span.set_attribute("aiva.quota.provenance", ctx.reservation.provenance.value)
        span.set_attribute("aiva.quota.lower_cost_route", bool(ctx.budget_notes))
        if ctx.budget_notes:
            span.set_attribute("aiva.quota.candidates", [m.id for m in pool])
        return pool

    def _replan(self, ctx: RunContext, pool: list[ModelSpec]) -> None:
        """RLCD re-plans within the narrowed, still policy-eligible pool after a lower-cost denial."""
        assert ctx.assessment is not None
        costs = self._request_costs(ctx, pool)
        plan = self.s.router.plan(self._router_request(ctx), ctx.assessment, pool, costs)
        ctx.plan = replace(plan, reasons=(*plan.reasons, *ctx.budget_notes))
        ctx.selected_model = self.s.catalog.model(plan.model_id)

    def _domain(self, ctx: RunContext) -> str:
        """The money domain this run reserves and commits in (ADR-0008 amendment)."""
        return run_domain(
            ctx.execution_mode,  # the run's mode (ADR-0019): only live and record runs spend money
            roles=ctx.identity.roles,
            pseudonym=ctx.identity.principal_pseudonym,
        )

    def _public_live(self, ctx: RunContext) -> bool:
        """A public visitor's live run: capped by the public-demo budget and the visitor's hourly limit."""
        return PUBLIC_VISITOR_ROLE in ctx.identity.roles and self._domain(ctx) == LIVE

    def _internet_live(self, ctx: RunContext) -> bool:
        """A live run of an application key admitted from outside its trusted networks (ADR-0027): it never
        wakes a scaled-to-zero endpoint, and the internet GPU budget caps its estimated GPU cost."""
        decision = ctx.request_decision
        internet = decision is not None and decision.matched_network_id == INTERNET_CALLER_NETWORK
        return internet and self._domain(ctx) == LIVE

    def _stays_asleep(self, ctx: RunContext) -> bool:
        """Runs that never wake a scaled-to-zero endpoint: a public visitor's live run (ADR-0019) and an
        internet key call's (ADR-0027). Each wake bills at least the 15-minute idle tail, which no per-run
        budget sees."""
        return (ctx.public_visitor and not ctx.per_run_replay) or self._internet_live(ctx)

    def _live_window(self) -> LiveRunWindow:
        settings = self.s.settings
        return LiveRunWindow(
            self.s.redis,
            limit=settings.public_live_runs_per_window,
            window=timedelta(seconds=settings.public_live_window_seconds),
        )

    async def _take_live_slot(self, ctx: RunContext) -> None:
        visitor = ctx.identity.principal_pseudonym
        try:
            if not visitor:
                raise VisitorLimitReached(None)
            await self._live_window().take(ctx.tenant_id, visitor, ctx.run_id)
        except VisitorLimitReached as exc:
            ctx.finish(
                RunStatus.BLOCKED,
                Outcome.QUOTA_DENIED,
                live_demo_unavailable(
                    "visitor_limit",
                    message="This visitor has used every live run of the hour.",
                    run_id=ctx.run_id,
                    resets_at=exc.resets_at,
                ),
            )
            raise _Halt("Visitor live-run limit reached") from exc

    def _quota_denied(self, ctx: RunContext, exc: QuotaDeniedError, amount_micros: int) -> None:
        visitor = PUBLIC_VISITOR_ROLE in ctx.identity.roles
        if visitor and self._domain(ctx) == LIVE:
            ctx.finish(
                RunStatus.BLOCKED,
                Outcome.QUOTA_DENIED,
                live_demo_unavailable(
                    "budget",
                    message=f"{exc} Public live runs pause until the budget resets.",
                    run_id=ctx.run_id,
                    resets_at=exc.resets_at,
                    rule_ids=[exc.rule_id],
                ),
            )
            raise _Halt("Public live budget exhausted") from exc
        # Public visitors cannot use the approval queue (ADR-0019): their denial is final. Nor is an approval
        # offered when the budget was already past its cap: it covers this request's share only, so it could
        # never admit it (ADR-0014 amendment).
        exhausted = exc.on_exhaustion == "approval_required" and exc.already_exhausted
        approval = exc.on_exhaustion == "approval_required" and not visitor and not exhausted
        message = str(exc)
        remediation = "Ask a budget owner to raise the cap or retry in the next period."
        if exc.on_exhaustion == LOWER_COST_ROUTE:
            message += " No eligible lower-cost route fits within it."
        if exhausted:
            message = f"{BUDGET_ALREADY_EXHAUSTED_COPY} {message}"
            remediation = "Ask a budget owner to raise the cap: no approval can admit a request past it."
        if approval:
            remediation = "A budget owner must approve this request in the AiVa Govern approval queue."
            # Becomes a `budget` approval request for the denied envelope (ADR-0012).
            request_input = next(
                (e.policy_input for e in reversed(ctx.policy_events) if e.stage is PolicyStage.REQUEST), None
            )
            ctx.budget_approval = BudgetApprovalRecord(
                approval_id=new_id("apr"),
                rule_id=exc.rule_id,
                message=message,
                amount_micros=amount_micros if exc.amount_micros is None else exc.amount_micros,
                expires_at=datetime.now(UTC) + timedelta(seconds=self.s.settings.approval_ttl_seconds),
                policy_input=request_input,
            )
            ctx.approval_id = ctx.budget_approval.approval_id
        ctx.finish(
            RunStatus.APPROVAL_REQUIRED if approval else RunStatus.BLOCKED,
            Outcome.APPROVAL_REQUIRED if approval else Outcome.QUOTA_DENIED,
            _error(
                "QUOTA_EXCEEDED",
                message,
                run=ctx.run_id,
                rule_ids=[exc.rule_id],
                remediation=remediation,
                details={"reason": BUDGET_ALREADY_EXHAUSTED} if exhausted else None,
            ),
        )
        raise _Halt("Budget reservation denied") from exc

    async def _preflight(self, ctx: RunContext, span: Span) -> None:
        model = ctx.selected_model
        assert model is not None
        if ctx.reservation is None or not ctx.reservation.covers(model.id):
            raise AivaError(
                "The selected model is not covered by the budget reservation.",
                code="RESERVATION_INSUFFICIENT",
            )
        bound = input_token_bound(ctx.upstream_messages)
        if bound > ctx.reserved_input_tokens and ctx.context_strategy != "full":
            # A verified package can exceed the byte bound only with dense non-ASCII text; the full eligible
            # context is always covered by the reservation, so restore it rather than fail the run.
            self._use_full_context(ctx)
            span.set_attribute("aiva.preflight.context_restored", True)
            bound = input_token_bound(ctx.upstream_messages)
        span.set_attribute("aiva.preflight.input_token_bound", bound)
        if bound > ctx.reserved_input_tokens:
            raise AivaError(
                "The upstream payload exceeds the reserved budget envelope.", code="RESERVATION_INSUFFICIENT"
            )
        submitted = sum(estimate_tokens(m["content"]) for m in ctx.upstream_messages)
        span.set_attribute("aiva.preflight.input_tokens", submitted)
        if submitted + ctx.request.max_output_tokens > model.context_window:
            ctx.finish(
                RunStatus.BLOCKED,
                Outcome.ERROR,
                _error(
                    "CONTEXT_WINDOW_EXCEEDED",
                    "The request does not fit the selected model's context window.",
                    run=ctx.run_id,
                    remediation="Reduce the context or max_output_tokens.",
                ),
            )
            raise _Halt("Context window exceeded")
        decision = await self._evaluate(
            ctx, PolicyStage.EGRESS, PolicyEgress(model_id=model.id, stage=AttemptStage.UPSTREAM)
        )
        span.set_attribute("aiva.preflight.egress", decision.action.value)
        if decision.action != PolicyAction.ALLOW:
            ctx.finish(
                RunStatus.BLOCKED,
                Outcome.POLICY_DENIED,
                _error(
                    "POLICY_DENIED",
                    "Blocked by AI policy at egress.",
                    run=ctx.run_id,
                    rule_ids=[r.rule_id for r in decision.reasons],
                ),
            )
            raise _Halt("Blocked by AI policy at egress")

    async def _inference(self, ctx: RunContext, span: Span) -> None:
        model = ctx.selected_model
        assert model is not None
        self._mark_web_sent(ctx, span)
        result = await self._call(ctx, model, AttemptStage.UPSTREAM)
        ctx.inference = result
        span.set_attribute("aiva.inference.model_id", model.id)
        span.set_attribute("aiva.inference.status", result.status)
        span.set_attribute("aiva.inference.provenance", result.provenance.value)
        if result.status != "ok":
            sovereign = ctx.path is ExecutionPath.SOVEREIGN_DIRECT
            code = "SOVEREIGN_ENDPOINT_UNAVAILABLE" if sovereign else "UPSTREAM_UNAVAILABLE"
            message = (
                "The sovereign model is unavailable. The request was not sent to any other model."
                if sovereign
                else "The selected model is unavailable."
            )
            ctx.finish(
                RunStatus.FAILED,
                Outcome.TIMEOUT if result.error_code == "TIMEOUT" else Outcome.ERROR,
                _error(code, message, run=ctx.run_id, retryable=True, remediation="Retry shortly."),
                http_status=503,
            )
            raise _Halt(code)
        if ctx.request.baseline_comparison:
            await self._baseline(ctx, span)

    async def _baseline(self, ctx: RunContext, span: Span) -> None:
        frontier = self.s.catalog.model(self.s.catalog.models.frontier_baseline)
        if frontier is None or ctx.selected_model is None:
            span.set_attribute("aiva.baseline.status", "not-needed")
            return
        if frontier.id == ctx.selected_model.id:
            # The answer already is the frontier model's: a comparison would be a second, identical-model
            # call that measures nothing. No call, no charge.
            span.set_attribute("aiva.baseline.status", "same-as-served")
            return
        decision = await self._evaluate(
            ctx, PolicyStage.EGRESS, PolicyEgress(model_id=frontier.id, stage=AttemptStage.BASELINE)
        )
        if decision.action != PolicyAction.ALLOW:
            span.set_attribute("aiva.baseline.status", "denied-by-policy")
            return
        reserved = ctx.reservation is not None and ctx.reservation.covers(frontier.id)
        if not reserved or not ctx.baseline_max_output_tokens:
            span.set_attribute("aiva.baseline.status", "not-reserved")
            return
        result = await self._call(ctx, frontier, AttemptStage.BASELINE, retries=False)
        span.set_attribute("aiva.baseline.status", result.status)
        span.set_attribute("aiva.baseline.max_output_tokens", ctx.baseline_max_output_tokens)
        if result.finish_reason:
            span.set_attribute("aiva.baseline.finish_reason", result.finish_reason)

    async def _call(
        self, ctx: RunContext, model: ModelSpec, stage: AttemptStage, *, retries: bool = True
    ) -> InferenceResult:
        adapter = self._adapters(ctx).for_model(model)
        baseline = stage is AttemptStage.BASELINE
        request = InferenceRequest(
            run_id=ctx.run_id,
            model=model,
            messages=ctx.upstream_messages,
            # The baseline's own budget, reserved with it: frontier thinking counts against the output cap.
            max_output_tokens=ctx.baseline_max_output_tokens if baseline else ctx.request.max_output_tokens,
            effort=self.s.catalog.models.baseline.effort if baseline else None,
            # The answering model's temperature for what the answer is grounded in (ADR-0009, 2026-10-22
            # grounded-answers amendment). The baseline is the frontier model's own answer, at its default.
            temperature=None if baseline else model.answer_temperature(self._grounding(ctx)),
        )
        charge = ChargeType.BASELINE_COMPARISON if baseline else ChargeType.UPSTREAM_INFERENCE
        started = datetime.now(UTC)
        result = await adapter.complete(request)
        first = self._record_attempt(ctx, stage, model, started, result, charge, retry_of=None)
        if result.status != "ok" and result.retryable and retries:
            # The retry is a new outbound call: policy is evaluated again at egress (Appendix B).
            egress = await self._evaluate(
                ctx, PolicyStage.EGRESS, PolicyEgress(model_id=model.id, stage=stage)
            )
            if egress.action != PolicyAction.ALLOW:
                return result
            started = datetime.now(UTC)
            result = await adapter.complete(request)
            self._record_attempt(
                ctx, stage, model, started, result, ChargeType.RETRY, retry_of=first.attempt_id
            )
        return result

    def _assurance(self, ctx: RunContext, span: Span) -> None:
        assert ctx.inference is not None
        required = self._decision(ctx).effective.output_dlp == "required"
        # The answer-citation check reads what the model was actually given; in-run only, never persisted.
        context = AnswerContext(
            sent="\n".join(m["content"] for m in ctx.upstream_messages),
            evidence_ids=tuple(e.id for e in ctx.evidence),
        )
        outcome = self.s.assurance.check(ctx.inference.content, output_dlp_required=required, context=context)
        ctx.assurance = outcome
        if outcome.citations is not None:
            span.set_attribute("aiva.assurance.unresolved_citations", outcome.citations.unresolved)
        span.set_attribute("aiva.assurance.passed", outcome.passed)
        span.set_attribute("aiva.assurance.output_dlp", outcome.output_dlp)
        span.set_attribute("aiva.assurance.finding_types", list(outcome.findings))
        if not outcome.passed:
            ctx.finish(
                RunStatus.BLOCKED,
                Outcome.QUALITY_FAIL,
                _error(
                    "OUTPUT_BLOCKED",
                    outcome.blocked_reason or "Output failed assurance checks.",
                    run=ctx.run_id,
                ),
            )
            return
        ctx.response_content = outcome.content
        ctx.response_provenance = ctx.inference.provenance
        ctx.finish(RunStatus.COMPLETED, Outcome.SUCCESS)

    async def _reconcile(self, ctx: RunContext, span: Span) -> None:
        if ctx.reservation is not None:
            try:
                ctx.released_micros = await self.s.quota.settle(
                    ctx.reservation,
                    ctx.api_cost_micros,
                    charges=settlement_charges(ctx.attempts, ctx.tool_charges),
                )
                ctx.reservation_settled = True
            except AivaError as exc:
                # The reservation stays open; ledger reconciliation settles it later.
                span.set_attribute("aiva.reconciliation.settle_error", exc.code)
        visitor = ctx.identity.principal_pseudonym
        if visitor and not ctx.attempts and self._public_live(ctx):
            # No model was called (refused, or stopped before inference): the live run is not counted.
            await self._live_window().give_back(ctx.tenant_id, visitor, ctx.run_id)
        cacheable = ctx.status is RunStatus.COMPLETED and ctx.path is not ExecutionPath.CACHE_RETURN
        final = ctx.classification.effective if ctx.classification else None
        if cacheable and ctx.cache_key and final is not ctx.cache_classification:
            # RLCD tightened the classification after the lookup: the key's scope is the lower one, so
            # storing would let a later lookup at that level receive this answer. Never stored.
            cacheable = False
            span.set_attribute("aiva.cache.store_skipped", "classification tightened after lookup")
        if (
            cacheable
            and ctx.cache_key
            and ctx.rlcd_prediction is None
            and getattr(self.s.rlcd, "ready", False)
        ):
            # The RLCD model is serving but missed this run (time budget, queue, error), so its classification
            # lacks the model's signal. Identical requests must not inherit that fallback from the cache.
            cacheable = False
            span.set_attribute("aiva.cache.store_skipped", "RLCD model missed this run")
        if cacheable and ctx.cache_key and ctx.selected_model and ctx.response_content:
            upstream = [a for a in ctx.attempts if a.stage is AttemptStage.UPSTREAM]
            await self.s.cache.store(
                ctx.cache_key,
                CachedResponse(
                    source_run_id=ctx.run_id,
                    model_id=ctx.selected_model.id,
                    content=ctx.response_content,
                    modeled_input_tokens=sum(a.input_tokens for a in upstream),
                    modeled_output_tokens=sum(a.output_tokens for a in upstream),
                    modeled_cost_micros=sum(a.cost_micros for a in upstream),
                    provenance=ctx.response_provenance,
                ),
                ttl_seconds=CACHE_TTL_SECONDS,
            )
        ctx.completed_at = datetime.now(UTC)
        span.set_attribute("aiva.reconciliation.api_cost_micros", ctx.api_cost_micros)
        span.set_attribute("aiva.reconciliation.released_micros", ctx.released_micros)
        span.set_attribute("aiva.status", ctx.status.value)

    # --- Helpers -----------------------------------------------------------------------------------

    def _adapters(self, ctx: RunContext) -> AdapterRegistry:
        """The run's adapter set (ADR-0019): per-run replay, or the gateway's configured mode."""
        return self.s.replay_adapters if ctx.per_run_replay else self.s.adapters

    def _cim_client(self, ctx: RunContext) -> ContextIntelligence:
        return self.s.replay_cim if ctx.per_run_replay else self.s.cim

    async def _awake(self, ctx: RunContext, model: ModelSpec) -> bool:
        """False only when a run that stays asleep (`_stays_asleep`) would wake a scaled-to-zero endpoint. The
        endpoint's state is read from the HF Endpoints API, never by calling the endpoint."""
        if not (self._stays_asleep(ctx) and model.provider == "hf-endpoint"):
            return True
        return await self.s.resolver.is_awake(model)

    def _plan(self, ctx: RunContext, eligible: list[ModelSpec]) -> ExecutionPlan:
        """RLCD's plan within the eligible set. It does not depend on the CIM's output."""
        assert ctx.assessment is not None
        costs = self._request_costs(ctx, eligible)
        return self.s.router.plan(self._router_request(ctx), ctx.assessment, eligible, costs)

    def _request_costs(self, ctx: RunContext, models: list[ModelSpec]) -> dict[str, int]:
        """What serving this request would cost on each model, GPU time included, sized on the full eligible
        context so the choice does not depend on the CIM's output."""
        full = input_token_bound(full_context_messages(ctx.messages, ctx.evidence))
        return request_costs(
            self.s.prices, models, input_tokens=full, max_output_tokens=ctx.request.max_output_tokens
        )

    async def _check_planned_path(self, ctx: RunContext, eligible: list[ModelSpec]) -> None:
        """A public visitor's live run (ADR-0019) or an internet key call's (ADR-0027) checks the endpoint
        its plan will call before the first billable stage, so it never spends on the CIM and is then refused
        for a sleeping Endpoint B. Once the path is fixed, the upstream RLCD will select is known: its plan
        does not depend on the CIM's output. A sleeping upstream endpoint refuses the run with nothing
        reserved or metered. The CIM endpoint's state is read next, in the CIM stage, before its reservation;
        asleep, the CIM is bypassed. Retrieved passages can still change what the answer is grounded in, and
        so the choice between qualified models (ADR-0009, 2026-10-22 grounded-answers amendment);
        `_never_wake` checks the final choice again."""
        if not eligible or not self._stays_asleep(ctx):
            return
        model = self.s.catalog.model(self._plan(ctx, eligible).model_id)
        if model is not None and not await self._awake(ctx, model):
            self._refuse_cold(ctx, trace.get_current_span())

    async def _never_wake(self, ctx: RunContext, span: Span) -> None:
        """Backstop at final routing: if the budget reservation narrowed the eligible set onto a sleeping
        endpoint, the run is refused before the upstream reservation and call. The orchestrator has no
        fallback for an unavailable upstream, so this holds on every path; the sovereign path never falls
        back in any case."""
        model = ctx.selected_model
        if model is not None and not await self._awake(ctx, model):
            self._refuse_cold(ctx, span)

    def _refuse_cold(self, ctx: RunContext, span: Span) -> None:
        """Refuse a run that stays asleep rather than wake a scaled-to-zero endpoint: a blocked run in FinOps'
        LIVE_DEMO_UNAVAILABLE shape (reason `endpoint_cold`, not retryable), never rerouted. It is not a
        quota."""
        span.set_attribute("aiva.endpoint.awake", False)
        if ctx.public_visitor:
            refusal = live_demo_unavailable(
                "endpoint_cold",
                message="The live model endpoint is asleep, and public live runs do not wake it.",
                run_id=ctx.run_id,
            )
        else:
            refusal = live_demo_unavailable(
                "endpoint_cold",
                message="The model endpoint is asleep, and key calls from the internet do not wake it.",
                run_id=ctx.run_id,
                remediation=(
                    "The endpoint wakes only for calls from the application's trusted networks; retry when "
                    "it is running."
                ),
            )
        ctx.finish(RunStatus.BLOCKED, Outcome.ERROR, refusal)
        raise _Halt("Live endpoint asleep")

    def _decision(self, ctx: RunContext) -> PolicyDecision:
        assert ctx.request_decision is not None
        return ctx.request_decision

    def _request_evidence(self, ctx: RunContext) -> list[CimEvidence]:
        return [CimEvidence(id=e.id, text=e.text) for e in ctx.request.evidence]

    def _grounding(self, ctx: RunContext) -> Grounding:
        """What the answer is grounded in: the full eligible evidence (supplied, retrieved and web passages)
        once retrieval ran, the supplied evidence before. Like RLCD's costs, never the CIM's selection."""
        return grounding(e.text for e in (ctx.evidence or ctx.request.evidence))

    def _use_full_context(self, ctx: RunContext) -> None:
        messages = full_context_messages(ctx.messages, ctx.evidence)
        ctx.upstream_messages = messages
        ctx.constructed_context_tokens = sum(estimate_tokens(m["content"]) for m in messages)
        ctx.context_strategy = "full"

    async def authorize_tool_call(
        self, ctx: RunContext, tool_id: str, *, arguments: Mapping[str, Any] | None = None
    ) -> PolicyDecision:
        """Evaluate the `tool` stage before a tool call (LLD §7 Tools/agents, ADR-0012).

        Call the tool only when the action is `allow`. On `approval_required` the run records an approval
        request (kind `tool`) when it is persisted. With `arguments`, that approval is bound to this exact
        call (`arguments_sha256`) and `govern.approvals.authorize_recorded_tool_call` can execute it later
        without a new run; otherwise a resubmission with the approval may call the tool.
        """
        if ctx.request_decision is None or ctx.request_decision.action != PolicyAction.ALLOW:
            raise AivaError(
                "Tool calls require an allowed request-stage decision.", code="POLICY_NOT_EVALUATED"
            )
        digest = tool_arguments_sha256(arguments) if arguments is not None else None
        return await self._evaluate(
            ctx, PolicyStage.TOOL, tool=PolicyTool(tool_id=tool_id, arguments_sha256=digest)
        )

    async def _evaluate(
        self,
        ctx: RunContext,
        stage: PolicyStage,
        egress: PolicyEgress | None = None,
        *,
        tool: PolicyTool | None = None,
    ) -> PolicyDecision:
        policy_input = build_policy_input(ctx, stage, egress=egress, tool=tool)
        decision = await self.s.policy_engine.evaluate(policy_input)
        assert ctx.classification is not None
        event = PolicyEventRecord(
            stage=stage,
            decision=decision,
            classification=ctx.classification,
            egress_model_id=egress.model_id if egress else None,
            reservation_id=ctx.reservation.reservation_id if ctx.reservation else None,
            tool_id=tool.tool_id if tool else None,
            policy_input=policy_input,
        )
        if decision.action == PolicyAction.APPROVAL_REQUIRED and stage is not PolicyStage.EGRESS:
            event.approval_id = new_id("apr")
            event.approval_expires_at = datetime.now(UTC) + timedelta(
                seconds=self.s.settings.approval_ttl_seconds
            )
        ctx.policy_events.append(event)
        return decision

    def _record_attempt(
        self,
        ctx: RunContext,
        stage: AttemptStage,
        model: ModelSpec,
        started: datetime,
        result: InferenceResult,
        charge: ChargeType,
        *,
        retry_of: str | None,
    ) -> AttemptRecord:
        usage = result.usage
        cost = 0
        if result.status == "ok" or usage.input_tokens or usage.output_tokens:
            cost = self.s.prices.cost_micros(
                model.id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_input_tokens=usage.cached_input_tokens,
            )
        # A replayed call's latency is the local replay's, not the endpoint's busy time: no GPU estimate.
        gpu = (
            self.s.prices.gpu_estimate_micros(model.id, result.latency_ms)
            if gpu_estimate_applies(result.provenance)
            else 0
        )
        record = AttemptRecord(
            attempt_id=attempt_id(),
            stage=stage,
            model=model,
            started_at=started,
            completed_at=datetime.now(UTC),
            status=result.status,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            latency_ms=result.latency_ms,
            ttft_ms=result.ttft_ms,
            provider_model_version=result.provider_model_version,
            provenance=result.provenance,
            error_code=result.error_code,
            retry_of=retry_of,
            charge_type=charge,
            cost_micros=cost,
            gpu_estimate_micros=gpu,
        )
        ctx.attempts.append(record)
        return record


def replace_classification(current: PolicyClassification, *, rlcd: Classification) -> PolicyClassification:
    """Add the RLCD prediction; the effective level can only rise."""
    effective = most_restrictive(current.effective, rlcd)
    return PolicyClassification(
        declared=current.declared, floor=current.floor, dlp=current.dlp, rlcd=rlcd, effective=effective
    )
