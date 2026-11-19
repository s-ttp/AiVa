"""Build the API response from a finished run."""

from datetime import UTC, datetime

from opentelemetry.sdk.trace import ReadableSpan

from aiva_gateway.contracts.chat import (
    AnswerCitations,
    ApprovalSummary,
    AssuranceSummary,
    BudgetSummary,
    CacheSummary,
    ChatResponse,
    CimSummary,
    CostSummary,
    IdentitySummary,
    ModelUsage,
    PolicySummary,
    ResponseMessage,
    RoutingSummary,
    SearchSource,
    SearchSummary,
    UsageSummary,
)
from aiva_gateway.contracts.runs import TraceSpanOut
from aiva_gateway.core.enums import (
    AttemptStage,
    ChargeType,
    Classification,
    ExecutionPath,
    PolicyAction,
    Provenance,
)
from aiva_gateway.finops.frontier import direct_frontier_estimate
from aiva_gateway.finops.ledger import gpu_estimate
from aiva_gateway.finops.pricing import PriceBook
from aiva_gateway.finops.scopes import least_trusted
from aiva_gateway.optimize.cim import passage_counts
from aiva_gateway.pipeline.context import RunContext
from aiva_gateway.pipeline.recorder import run_provenance, span_rows


def build_chat_response(ctx: RunContext, spans: list[ReadableSpan], *, prices: PriceBook) -> ChatResponse:
    decision = ctx.request_decision
    classification = ctx.classification
    provenance = run_provenance(ctx)
    upstream = [a for a in ctx.attempts if a.stage is AttemptStage.UPSTREAM]
    cim = [a for a in ctx.attempts if a.stage is AttemptStage.CIM]
    baseline = [a for a in ctx.attempts if a.stage is AttemptStage.BASELINE]
    successful = [a for a in upstream if a.status == "ok"]
    final = successful[-1] if successful else None

    usage = UsageSummary(
        original_context_tokens=ctx.context_tokens_before,
        constructed_context_tokens=ctx.constructed_context_tokens,
        upstream_submitted_input_tokens=final.input_tokens if final else 0,
        upstream_output_tokens=final.output_tokens if final else 0,
        cached_input_tokens=final.cached_input_tokens if final else 0,
        reasoning_tokens=final.reasoning_tokens if final else 0,
        cim_input_tokens=sum(a.input_tokens for a in cim),
        cim_output_tokens=sum(a.output_tokens for a in cim),
        retry_tokens=sum(
            a.input_tokens + a.output_tokens for a in upstream if a.charge_type is ChargeType.RETRY
        ),
        provenance=provenance,
        by_model=[
            ModelUsage(
                model_id=a.model.id,
                stage=a.stage,
                input_tokens=a.input_tokens,
                output_tokens=a.output_tokens,
                provenance=a.provenance,
            )
            for a in ctx.attempts
            if a.input_tokens or a.output_tokens
        ],
    )
    upstream_micros = sum(a.cost_micros for a in upstream)
    cim_micros = sum(a.cost_micros for a in cim)
    gpu_micros, gpu_provenance = gpu_estimate(ctx.attempts, prices.pools)
    direct_micros, direct_provenance = direct_frontier_estimate(
        prices,
        served=final,
        context_before=ctx.context_tokens_before,
        context_after=ctx.constructed_context_tokens,
        cache_hit=ctx.cache_hit_entry if ctx.path is ExecutionPath.CACHE_RETURN else None,
    )
    cost = CostSummary(
        upstream_micros=upstream_micros,
        cim_micros=cim_micros,
        baseline_micros=sum(a.cost_micros for a in baseline),
        gpu_estimated_micros=gpu_micros,
        gpu_estimated_provenance=gpu_provenance,
        tool_micros=ctx.tool_cost_micros,
        tool_provenance=least_trusted(c.provenance for c in ctx.tool_charges),
        total_api_micros=ctx.api_cost_micros,
        direct_frontier_estimated_micros=direct_micros,
        direct_frontier_provenance=direct_provenance,
        rate_card_version=prices.version,
        provenance=provenance,
    )
    effective = classification.effective if classification else Classification.INTERNAL
    policy = PolicySummary(
        action=decision.action if decision else PolicyAction.DENY,
        policy_version=decision.policy_version if decision else "",
        bundle_hash=decision.bundle_hash if decision else "",
        matched_rule_ids=decision.matched_rule_ids if decision else [],
        declared_classification=ctx.request.data_classification,
        effective_classification=effective,
        allowed_models=decision.effective.allowed_models if decision else [],
        denied_candidates=decision.denied_candidates if decision else [],
        reasons=decision.reasons if decision else [],
    )
    assessment, plan = ctx.assessment, ctx.plan
    routing = None
    if assessment is not None:
        routing = RoutingSummary(
            rlcd_version=plan.rlcd_version if plan else assessment.rlcd_version,
            task_category=assessment.task_category,
            complexity=assessment.complexity,
            confidence=plan.confidence if plan else assessment.confidence,
            context_strategy=ctx.context_strategy,
            reasons=[*assessment.reasons, *(plan.reasons if plan else ())],
        )
    assurance = ctx.assurance
    response = None
    if ctx.response_content is not None:
        response = ResponseMessage(
            content=ctx.response_content,
            finish_reason=ctx.inference.finish_reason if ctx.inference else "cache",
            provenance=ctx.response_provenance,
        )
    trace = None
    if ctx.request.trace:
        trace = [
            TraceSpanOut(
                span_id=row.span_id,
                parent_span_id=row.parent_span_id,
                name=row.name,
                status=row.status,
                started_at=row.started_at or datetime.now(UTC),
                duration_ms=row.duration_ms,
                attributes=row.attributes,
            )
            for row in span_rows(ctx, spans)
        ]
    return ChatResponse(
        run_id=ctx.run_id,
        trace_id=ctx.trace_id,
        status=ctx.status,
        outcome=ctx.outcome,
        selected_model=ctx.selected_model.id
        if ctx.selected_model and ctx.path is not ExecutionPath.NONE
        else None,
        execution_path=ctx.path,
        cim_invoked=bool(cim),
        response=response,
        usage=usage,
        cost=cost,
        policy=policy,
        identity=IdentitySummary(
            application_id=ctx.identity.application_id,
            identity_mode=ctx.identity.identity_mode,
            source_mode=ctx.identity.source_mode,
            matched_network_id=decision.matched_network_id if decision else None,
        ),
        routing=routing,
        cache=_cache_summary(ctx, upstream_call=bool(upstream)),
        cim=_cim_summary(ctx),
        budget=BudgetSummary(
            reservation_id=ctx.reservation.reservation_id if ctx.reservation else None,
            reserved_micros=ctx.reservation.amount_micros if ctx.reservation else 0,
            committed_micros=ctx.api_cost_micros if ctx.reservation else 0,
            released_micros=ctx.released_micros,
            provenance=ctx.reservation.provenance if ctx.reservation else provenance,
        ),
        assurance=AssuranceSummary(
            passed=assurance.passed if assurance else False,
            output_dlp=assurance.output_dlp if assurance else "not-run",
            checks=list(assurance.checks) if assurance else [],
            answer_citations=_answer_citations(ctx),
        ),
        approval=_approval_summary(ctx),
        search=_search_summary(ctx),
        data_designation=ctx.data_designation,
        execution_mode=ctx.execution_mode or None,
        error=ctx.error,
        trace=trace,
    )


def _cache_summary(ctx: RunContext, *, upstream_call: bool) -> CacheSummary:
    """On a hit, the tokens the answer's original run used: what this request did not spend. They are a
    counterfactual, so never better than estimated (simulated or replayed when that run was)."""
    hit = ctx.cache_hit_entry
    return CacheSummary(
        checked=ctx.cache_checked,
        hit=hit is not None,
        cache_type=(ctx.cache_type or "exact") if ctx.cache_checked else None,
        upstream_call=upstream_call,
        avoided_input_tokens=hit.modeled_input_tokens if hit else None,
        avoided_output_tokens=hit.modeled_output_tokens if hit else None,
        avoided_provenance=least_trusted([hit.provenance, Provenance.ESTIMATED]) if hit else None,
    )


def _cim_summary(ctx: RunContext) -> CimSummary:
    """Passage counts for the run view: retrieved once a retrieval stage ran; kept and omitted when the CIM
    returned a package (`passage_counts`, the rule the run record's `context_package` lists follow)."""
    package = ctx.cim_result.response if ctx.cim_result else None
    kept = omitted = None
    if ctx.cim_request is not None and package is not None:
        counts = passage_counts([e.id for e in ctx.cim_request.evidence], package.selected_evidence_ids)
        kept, omitted = counts.kept, counts.omitted
    return CimSummary(
        state=ctx.cim_state,
        message=ctx.cim_message,
        cim_revision=ctx.cim_result.revision if ctx.cim_result else None,
        sufficiency=ctx.sufficiency,
        passages_retrieved=ctx.passages_retrieved,
        passages_kept=kept,
        passages_omitted=omitted,
    )


def _search_summary(ctx: RunContext) -> SearchSummary | None:
    record = ctx.web_search
    if record is None:
        return None
    return SearchSummary.model_validate(
        {
            "provider": record.provider,
            "status": record.status,
            "detail": record.detail,
            "attempts": record.attempts,
            "results_returned": record.results_returned,
            "results_used": record.results_used,
            "results_sent": record.results_sent,
            "results_dropped_injection": record.dropped_injection,
            "results_dropped_dlp": record.dropped_dlp,
            "rule_ids": record.rule_ids,
            "sources": [
                SearchSource(title=s.title, url=s.url, evidence_id=s.evidence_id, sent_to_model=s.sent)
                for s in record.sources
            ],
        }
    )


def _answer_citations(ctx: RunContext) -> AnswerCitations:
    """Checked only when an answer was delivered from an upstream payload; otherwise `not_checked`."""
    check = ctx.assurance.citations if ctx.assurance and ctx.assurance.passed else None
    if check is None:
        return AnswerCitations(status="not_checked")
    return AnswerCitations(
        status=check.status,
        cited=check.cited,
        unresolved=check.unresolved,
        unresolved_refs=list(check.unresolved_refs),
    )


def _approval_summary(ctx: RunContext) -> ApprovalSummary:
    satisfied_by = ctx.approval.approval_id if ctx.approval else None
    budget = ctx.budget_approval
    if budget is not None and budget.approval_id == ctx.approval_id:
        return ApprovalSummary(
            approval_id=budget.approval_id,
            status="pending",
            expires_at=budget.expires_at,
            rule_ids=[budget.rule_id],
            satisfied_by=satisfied_by,
        )
    event = next((e for e in ctx.policy_events if e.approval_id and e.approval_id == ctx.approval_id), None)
    if event is None:
        return ApprovalSummary(satisfied_by=satisfied_by)
    return ApprovalSummary(
        approval_id=event.approval_id,
        status="pending",
        expires_at=event.approval_expires_at,
        rule_ids=[r.rule_id for r in event.decision.reasons],
        satisfied_by=satisfied_by,
    )
