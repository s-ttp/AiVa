"""Persist a finished run: run record, classification, policy events, attempts, ledger, spans (ADR-0003).

Everything is written in one transaction. No prompt or response text is stored here.
"""

from datetime import UTC, datetime
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan
from sqlalchemy.ext.asyncio import AsyncSession

from aiva_gateway.assurance.citations import evidence_coverage
from aiva_gateway.catalog import Catalog
from aiva_gateway.core.canonical import sha256_hex
from aiva_gateway.core.enums import ExecutionPath, Provenance
from aiva_gateway.db import models as db
from aiva_gateway.finops.ledger import record_reservation, usage_rows
from aiva_gateway.govern.approvals import approval_rows
from aiva_gateway.pipeline.context import RunContext
from aiva_gateway.telemetry.tracing import format_span_id, redact_attributes

_PROVENANCE_ORDER = (
    Provenance.SIMULATED,
    Provenance.REPLAYED,
    Provenance.SYNTHETIC,
    Provenance.ESTIMATED,
    Provenance.MEASURED,
)


def detect_language(text: str) -> str:
    arabic = sum(1 for ch in text if "؀" <= ch <= "ۿ")
    letters = sum(1 for ch in text if ch.isalpha())
    if not letters or arabic == 0:
        return "english"
    return "arabic" if arabic / letters > 0.8 else "bilingual"


def run_provenance(ctx: RunContext) -> Provenance:
    """The least trustworthy provenance among the run's measured components."""
    labels = [a.provenance for a in ctx.attempts]
    if ctx.reservation is not None:
        labels.append(ctx.reservation.provenance)
    if ctx.path is ExecutionPath.CACHE_RETURN:
        labels.append(ctx.response_provenance)
    if not labels:
        return Provenance.MEASURED
    return min(labels, key=_PROVENANCE_ORDER.index)


def _guard_details(ctx: RunContext) -> dict[str, Any]:
    """The Guard's verdict and how sufficiency was established (ids only, never passage text)."""
    if ctx.guard is None:
        return {}
    details: dict[str, Any] = {
        "failures": list(ctx.guard.failures),
        "failure_codes": list(ctx.guard.failure_codes),
        "checks": list(ctx.guard.checks),
    }
    if ctx.guard.passed:
        unverified = list(ctx.guard.relevance_unverified)
        verified = list(ctx.guard.relevance_verified)
        if unverified:
            basis = (
                "relevance-unverified: the CIM omitted passages the Guard could not rule out; "
                "full context sent"
            )
        elif verified:
            basis = (
                "question-term-verified: no omitted passage shares a term with the question or a document "
                "with a kept passage"
            )
        else:
            basis = "guard-verified"
        details["sufficiency_basis"] = basis
        details["relevance_unverified"] = {"omitted_passages": len(unverified), "ids": unverified}
        details["relevance_verified"] = {"omitted_passages": len(verified), "ids": verified}
    if ctx.recall_floor_ids:
        details["recall_floor_added"] = list(ctx.recall_floor_ids)
    if ctx.relevance_put_back:
        details["relevance_put_back"] = list(ctx.relevance_put_back)
    return details


def span_rows(ctx: RunContext, spans: list[ReadableSpan]) -> list[db.TraceSpanRow]:
    rows = []
    for span in sorted(spans, key=lambda s: s.start_time or 0):
        if span.context is None or span.start_time is None or span.end_time is None:
            continue
        rows.append(
            db.TraceSpanRow(
                span_id=format_span_id(span.context.span_id),
                trace_id=ctx.trace_id,
                run_id=ctx.run_id,
                parent_span_id=format_span_id(span.parent.span_id) if span.parent else None,
                name=span.name,
                status=str(
                    dict(span.attributes or {}).get("aiva.stage.status", span.status.status_code.name.lower())
                ),
                started_at=datetime.fromtimestamp(span.start_time / 1e9, tz=UTC),
                duration_ms=(span.end_time - span.start_time) / 1e6,
                attributes=redact_attributes(span.attributes),
            )
        )
    return rows


async def persist_run(
    session: AsyncSession, catalog: Catalog, ctx: RunContext, spans: list[ReadableSpan]
) -> None:
    classification = ctx.classification
    effective = classification.effective.value if classification else "internal"
    decision = ctx.request_decision
    session.add(
        db.ExecutionRunRow(
            run_id=ctx.run_id,
            trace_id=ctx.trace_id,
            tenant_id=ctx.tenant_id,
            application_id=ctx.identity.application_id,
            department_id=ctx.department_id,
            cost_center_id=ctx.cost_center_id,
            principal_kind=ctx.identity.principal_kind,
            principal_pseudonym=ctx.identity.principal_pseudonym,
            identity_mode=ctx.identity.identity_mode.value,
            source_mode=ctx.identity.source_mode.value,
            ingress_source_policy=decision.matched_network_id if decision else None,
            scenario_id=ctx.request.scenario_id,
            routing_mode=ctx.request.routing.mode,
            status=ctx.status.value,
            outcome=ctx.outcome.value,
            execution_path=ctx.path.value,
            selected_model=ctx.selected_model.id if ctx.selected_model else None,
            cim_state=ctx.cim_state.value,
            cache_hit=ctx.path is ExecutionPath.CACHE_RETURN,
            policy_version=decision.policy_version if decision else None,
            effective_classification=effective,
            environment=ctx.environment,
            data_designation=ctx.data_designation,
            provenance=run_provenance(ctx).value,
            error_code=ctx.error.code if ctx.error else None,
            request_hash=ctx.request_hash,
            # The served answer's hash, never its text: a comparison is bound to it (ADR-0023).
            answer_sha256=sha256_hex(ctx.response_content) if ctx.response_content is not None else None,
            execution_mode=ctx.execution_mode or None,
            created_at=ctx.created_at,
            completed_at=ctx.completed_at,
            latency_ms=ctx.latency_ms,
        )
    )
    await session.flush()

    assessment = ctx.assessment
    hinted = bool(ctx.request.task or ctx.scenario)
    unclassified = set(assessment.unclassified) if assessment else set()
    confidences: dict[str, Any] = {}
    if assessment and assessment.confidences:
        # Calibrated RLCD head confidences: model outputs, labelled measured (LLD §21.3).
        confidences = {**assessment.confidences, "provenance": Provenance.MEASURED.value}
    if assessment and assessment.confidence is not None:
        confidences["rlcd"] = assessment.confidence
    session.add(
        db.ClassificationEventRow(
            run_id=ctx.run_id,
            taxonomy_version=catalog.taxonomy.version,
            classifier_version=assessment.rlcd_version if assessment else "not-reached",
            business_function=ctx.business_function,
            # A head below its confidence threshold is recorded as unknown, which reports as `unclassified`.
            task_category=(assessment.task_category if "task_category" not in unclassified else None)
            if assessment
            else ctx.request.task,
            intent=ctx.scenario.business_intent if ctx.scenario else None,
            complexity=assessment.complexity if assessment and "complexity" not in unclassified else None,
            sensitivity=effective,
            language=detect_language(" ".join(m["content"] for m in ctx.messages)),
            confidences=confidences,
            review_needed=not hinted
            and (assessment is None or assessment.confidence is None or bool(unclassified)),
        )
    )

    for sequence, event in enumerate(ctx.policy_events):
        session.add(
            db.PolicyEventRow(
                run_id=ctx.run_id,
                sequence=sequence,
                stage=event.stage.value,
                egress_model_id=event.egress_model_id,
                tool_id=event.tool_id,
                action=event.decision.action.value,
                policy_version=event.decision.policy_version,
                bundle_hash=event.decision.bundle_hash,
                matched_rule_ids=event.decision.matched_rule_ids,
                reasons=[r.model_dump(mode="json") for r in event.decision.reasons],
                effective=event.decision.effective.model_dump(mode="json"),
                declared_classification=event.classification.declared.value
                if event.classification.declared
                else None,
                dlp_classification=event.classification.dlp.value if event.classification.dlp else None,
                effective_classification=event.classification.effective.value,
                matched_network_id=event.decision.matched_network_id,
                reservation_id=event.reservation_id,
            )
        )

    await record_reservation(session, catalog, ctx)

    await session.flush()
    for sequence, attempt in enumerate(ctx.attempts):
        session.add(
            db.ModelAttemptRow(
                attempt_id=attempt.attempt_id,
                run_id=ctx.run_id,
                sequence=sequence,
                stage=attempt.stage.value,
                model_id=attempt.model.id,
                provider=attempt.model.provider,
                provider_model_version=attempt.provider_model_version,
                retry_of=attempt.retry_of,
                status=attempt.status,
                input_tokens=attempt.input_tokens,
                output_tokens=attempt.output_tokens,
                cached_input_tokens=attempt.cached_input_tokens,
                reasoning_tokens=attempt.reasoning_tokens,
                latency_ms=attempt.latency_ms,
                ttft_ms=attempt.ttft_ms,
                provenance=attempt.provenance.value,
                error_code=attempt.error_code,
                started_at=attempt.started_at,
                completed_at=attempt.completed_at,
            )
        )
    await session.flush()
    session.add_all(usage_rows(ctx, catalog))

    if ctx.cache_checked and ctx.cache_key_hash and ctx.cache_scope:
        hit = ctx.cache_hit_entry
        session.add(
            db.CacheEventRow(
                run_id=ctx.run_id,
                cache_type=ctx.cache_type or "exact",
                scope=ctx.cache_scope,
                hit=ctx.path is ExecutionPath.CACHE_RETURN,
                key_hash=ctx.cache_key_hash,
                source_run_id=hit.source_run_id if hit else None,
            )
        )

    session.add(
        db.OptimizationEventRow(
            run_id=ctx.run_id,
            cache_type=(ctx.cache_type or "exact") if ctx.cache_checked else None,
            cache_hit=ctx.path is ExecutionPath.CACHE_RETURN,
            context_strategy=ctx.context_strategy,
            context_tokens_before=ctx.context_tokens_before,
            context_tokens_after=ctx.constructed_context_tokens,
            removed_passages_count=len(ctx.cim_result.response.omitted_evidence_ids)
            if ctx.cim_result and ctx.cim_result.response
            else 0,
            preservation_result=ctx.sufficiency,
            avoided_call=ctx.cache_hit_entry is not None,
            modeled_avoided_tokens=ctx.cache_hit_entry.modeled_input_tokens
            + ctx.cache_hit_entry.modeled_output_tokens
            if ctx.cache_hit_entry
            else 0,
            modeled_avoided_micros=ctx.cache_hit_entry.modeled_cost_micros if ctx.cache_hit_entry else 0,
        )
    )

    if ctx.cim_request is not None:
        response = ctx.cim_result.response if ctx.cim_result else None
        session.add(
            db.ContextPackageRow(
                run_id=ctx.run_id,
                cim_revision=ctx.cim_result.revision if ctx.cim_result else "none",
                context_profile=ctx.cim_request.context_profile,
                evidence_refs=[e.id for e in ctx.cim_request.evidence],
                retained_ids=response.selected_evidence_ids if response else [],
                removed_ids=response.omitted_evidence_ids if response else [],
                missing_fields=response.missing_fields if response else [],
                validation=ctx.sufficiency,
                guard_details=_guard_details(ctx),
                token_counts={
                    "original": ctx.context_tokens_before,
                    "constructed": ctx.constructed_context_tokens,
                },
                provenance=ctx.cim_result.provenance.value if ctx.cim_result else Provenance.SIMULATED.value,
            )
        )

    if ctx.assurance is not None:
        session.add(
            db.AssuranceResultRow(
                run_id=ctx.run_id,
                passed=ctx.assurance.passed,
                output_dlp=ctx.assurance.output_dlp,
                checks=list(ctx.assurance.checks),
                # Resolved answer citations ÷ cited (ADR-0009); null when nothing is cited or not checked.
                evidence_coverage=evidence_coverage(
                    ctx.assurance.citations if ctx.assurance.passed else None
                ),
                escalation=ctx.assurance.blocked_reason,
            )
        )

    for approval in approval_rows(ctx):
        session.add(approval)
        session.add(
            db.AuditLogRow(
                tenant_id=ctx.tenant_id,
                actor_kind=ctx.identity.principal_kind,
                actor_id=ctx.identity.principal_pseudonym or ctx.identity.application_id,
                action="approval.request",
                target_type="approval_request",
                target_id=approval.id,
                details={"run_id": ctx.run_id, "kind": approval.kind, "rule_ids": approval.rule_ids},
            )
        )

    if decision is not None and decision.action.value != "allow":
        session.add(
            db.AuditLogRow(
                tenant_id=ctx.tenant_id,
                actor_kind=ctx.identity.principal_kind,
                actor_id=ctx.identity.principal_pseudonym or ctx.identity.application_id,
                action=f"policy.{decision.action.value}",
                target_type="run",
                target_id=ctx.run_id,
                details={
                    "rule_ids": [r.rule_id for r in decision.reasons],
                    "policy_version": decision.policy_version,
                },
            )
        )

    search = ctx.web_search
    if search is not None and search.status != "unavailable":
        # ADR-0022: who searched, for which application, how many results and the outcome; never the question,
        # a title or a URL.
        session.add(
            db.AuditLogRow(
                tenant_id=ctx.tenant_id,
                actor_kind=ctx.identity.principal_kind,
                actor_id=ctx.identity.principal_pseudonym or ctx.identity.application_id,
                action="tool.web_search",
                target_type="run",
                target_id=ctx.run_id,
                details={
                    "application_id": ctx.identity.application_id,
                    "provider": search.provider,
                    "outcome": search.status,
                    "result_count": search.results_used,
                    "results_sent": search.results_sent,
                    "results_returned": search.results_returned,
                    "dropped_injection": search.dropped_injection,
                    "dropped_dlp": search.dropped_dlp,
                    "attempts": search.attempts,
                    "rule_ids": search.rule_ids,
                    "error_code": search.error_code,
                },
            )
        )

    session.add_all(span_rows(ctx, spans))
