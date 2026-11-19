"""Authorized run drilldown by trace ID (LLD §21.6 `GET /v1/reports/runs/{trace_id}`).

Returns the attempt-level usage, ledger charges, classification, policy decisions, optimization and
validation outcomes of one run, plus its redacted span timeline. Never prompt or response text. A run outside
the caller's scope is reported as not found, so its existence is not disclosed.
"""

from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aiva_gateway.contracts.reports import (
    LedgerCharge,
    ReportAttempt,
    ReportClassification,
    ReportOptimization,
    ReportPolicyEvent,
    ReportReservation,
    ReportRunDetail,
    ReportRunFacts,
    ReportSpan,
    ReportValidation,
)
from aiva_gateway.core.errors import NotFoundError
from aiva_gateway.db import models as db
from aiva_gateway.reporting.aggregate import load_run_facts
from aiva_gateway.reporting.dimensions import UNCLASSIFIED, principal_of
from aiva_gateway.reporting.lines import Pricing, run_label, run_lines
from aiva_gateway.reporting.scope import ReportScope, shown_principal
from aiva_gateway.reporting.totals import Totals


async def run_detail(
    session: AsyncSession, scope: ReportScope, trace_id: str, *, threshold: float, pricing: Pricing
) -> ReportRunDetail:
    run = await session.scalar(select(db.ExecutionRunRow).where(db.ExecutionRunRow.trace_id == trace_id))
    if run is None or not scope.allows(
        tenant_id=run.tenant_id, department_id=run.department_id, application_id=run.application_id
    ):
        raise NotFoundError("Run not found.", remediation="Check the trace ID and your reporting scope.")
    facts = (await load_run_facts(session, threshold, db.ExecutionRunRow.run_id == run.run_id))[run.run_id]
    usage = Totals().extend(run_lines(facts, pricing)).usage()

    attempts = list(
        await session.scalars(
            select(db.ModelAttemptRow)
            .where(db.ModelAttemptRow.run_id == run.run_id)
            .order_by(db.ModelAttemptRow.sequence)
        )
    )
    charges = [
        LedgerCharge(
            ledger_id=row.ledger_id,
            attempt_id=row.attempt_id,
            charge_type=row.charge_type,
            amount_micros=row.amount_micros,
            provenance=row.measured_or_estimated,
            rate_card_version=row.rate_card_version,
            created_at=row.created_at,
        )
        for row in await session.scalars(
            select(db.UsageLedgerRow)
            .where(db.UsageLedgerRow.run_id == run.run_id)
            .order_by(db.UsageLedgerRow.created_at, db.UsageLedgerRow.ledger_id)
        )
    ]
    own = {a.attempt_id for a in attempts}
    attempt_out = [
        ReportAttempt(
            attempt_id=a.attempt_id,
            stage=a.stage,
            model_id=a.model_id,
            provider=a.provider,
            provider_model_version=a.provider_model_version,
            status=a.status,
            retry_of=a.retry_of,
            input_tokens=a.input_tokens,
            output_tokens=a.output_tokens,
            cached_input_tokens=a.cached_input_tokens,
            reasoning_tokens=a.reasoning_tokens,
            latency_ms=a.latency_ms,
            provenance=a.provenance,
            error_code=a.error_code,
            charges=[c for c in charges if c.attempt_id == a.attempt_id and c.charge_type != "adjustment"],
        )
        for a in attempts
    ]
    other = [c for c in charges if c.attempt_id not in own or c.charge_type == "adjustment"]

    policy = [
        ReportPolicyEvent(
            stage=e.stage,
            action=e.action,
            policy_version=e.policy_version,
            bundle_hash=e.bundle_hash,
            matched_rule_ids=list(e.matched_rule_ids),
            effective_classification=e.effective_classification,
            egress_model_id=e.egress_model_id,
            tool_id=e.tool_id,
        )
        for e in await session.scalars(
            select(db.PolicyEventRow)
            .where(db.PolicyEventRow.run_id == run.run_id)
            .order_by(db.PolicyEventRow.sequence)
        )
    ]
    event = await session.scalar(
        select(db.ClassificationEventRow)
        .where(db.ClassificationEventRow.run_id == run.run_id)
        .order_by(db.ClassificationEventRow.id.desc())
        .limit(1)
    )
    optimization = await session.scalar(
        select(db.OptimizationEventRow)
        .where(db.OptimizationEventRow.run_id == run.run_id)
        .order_by(db.OptimizationEventRow.id.desc())
        .limit(1)
    )
    assurance = await session.scalar(
        select(db.AssuranceResultRow)
        .where(db.AssuranceResultRow.run_id == run.run_id)
        .order_by(db.AssuranceResultRow.id.desc())
        .limit(1)
    )
    package = await session.scalar(
        select(db.ContextPackageRow)
        .where(db.ContextPackageRow.run_id == run.run_id)
        .order_by(db.ContextPackageRow.id.desc())
        .limit(1)
    )
    reservation = await session.scalar(
        select(db.BudgetReservationRow)
        .where(db.BudgetReservationRow.run_id == run.run_id)
        .order_by(db.BudgetReservationRow.created_at)
        .limit(1)
    )
    spans = await session.scalars(
        select(db.TraceSpanRow)
        .where(db.TraceSpanRow.run_id == run.run_id)
        .order_by(db.TraceSpanRow.started_at)
    )
    dims = facts.dimensions
    return ReportRunDetail(
        run=ReportRunFacts(
            run_id=run.run_id,
            trace_id=run.trace_id,
            tenant_id=run.tenant_id,
            application_id=run.application_id,
            department_id=run.department_id,
            cost_center_id=run.cost_center_id,
            principal_kind=run.principal_kind,
            principal=shown_principal(
                scope, principal_of(run.principal_kind, run.principal_pseudonym, run.application_id)
            ),
            identity_mode=run.identity_mode,
            source_mode=run.source_mode,
            source_policy=run.ingress_source_policy,
            scenario_id=run.scenario_id,
            status=run.status,
            outcome=run.outcome,
            execution_path=run.execution_path,
            cim_state=run.cim_state,
            cache_hit=run.cache_hit,
            selected_model=run.selected_model,
            effective_classification=run.effective_classification,
            policy_version=run.policy_version,
            environment=run.environment,
            data_designation=run.data_designation,
            provenance=run_label(run.provenance, run.data_designation),
            error_code=run.error_code,
            created_at=run.created_at,
            completed_at=run.completed_at,
            latency_ms=run.latency_ms,
        ),
        classification=ReportClassification(
            taxonomy_version=event.taxonomy_version,
            classifier_version=event.classifier_version,
            business_function=event.business_function,
            predicted_task_category=event.task_category,
            intent=event.intent,
            predicted_complexity=event.complexity,
            sensitivity=event.sensitivity,
            language=event.language,
            confidence=_confidence(event.confidences),
            review_needed=event.review_needed,
            reviewed=event.reviewed_label is not None,
            reported_task_category=dims["task_category"],
            reported_complexity=dims["complexity"],
            status=_status(dims["classification_status"]),
        )
        if event is not None
        else None,
        usage=usage,
        attempts=attempt_out,
        other_charges=other,
        policy_events=policy,
        optimization=ReportOptimization(
            cache_type=optimization.cache_type,
            cache_hit=optimization.cache_hit,
            context_strategy=optimization.context_strategy,
            context_tokens_before=optimization.context_tokens_before,
            context_tokens_after=optimization.context_tokens_after,
            removed_passages=optimization.removed_passages_count,
            preservation_result=optimization.preservation_result,
            avoided_call=optimization.avoided_call,
            modeled_avoided_tokens=optimization.modeled_avoided_tokens,
            modeled_avoided_micros=optimization.modeled_avoided_micros,
        )
        if optimization is not None
        else None,
        validation=ReportValidation(
            assurance_passed=assurance.passed if assurance else None,
            output_dlp=assurance.output_dlp if assurance else None,
            checks=[str(c) for c in assurance.checks] if assurance else [],
            context_validation=package.validation if package else None,
            missing_fields=list(package.missing_fields) if package else [],
        ),
        reservation=ReportReservation(
            reservation_id=reservation.reservation_id,
            estimate_micros=reservation.estimate_micros,
            actual_micros=reservation.actual_micros,
            released_micros=reservation.released_micros,
            status=reservation.status,
            provenance=reservation.provenance,
        )
        if reservation is not None
        else None,
        spans=[
            ReportSpan(
                name=s.name,
                status=s.status,
                started_at=s.started_at,
                duration_ms=s.duration_ms,
                skip_reason=str(s.attributes.get("aiva.stage.skip_reason"))
                if s.attributes.get("aiva.stage.skip_reason") is not None
                else None,
            )
            for s in spans
        ],
    )


def _confidence(confidences: dict[str, object]) -> float | None:
    value = confidences.get("rlcd")
    return float(value) if isinstance(value, int | float) else None


def _status(value: str) -> Literal["classified", "unclassified", "reviewed"]:
    if value == "reviewed":
        return "reviewed"
    return "unclassified" if value == UNCLASSIFIED else "classified"
