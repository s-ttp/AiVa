"""RLCD routing (LLD §10): the ModernBERT classifier's calibrated predictions, with the P0 heuristic as the
fallback whenever no prediction is available.

It only chooses within the policy-eligible set it is given; hard constraints are never its decision. The
classifier's sensitivity can only tighten the effective classification (ADR-0005); the orchestrator applies
it and re-evaluates policy. Among the qualified models it picks the lowest estimated cost per request, an
in-house model's GPU time included (`request_costs`, ADR-0009 2026-10-22 amendment). A model qualifies by the
quality tier and, for a grounded request, by what the answer is grounded in (`grounding`, ADR-0009 2026-10-22
grounded-answers amendment).
"""

from collections.abc import Iterable
from dataclasses import replace

from aiva_gateway.catalog.models import MIN_QUALITY_RANK, Grounding, ModelSpec
from aiva_gateway.core.enums import Classification
from aiva_gateway.finops.pricing import PriceBook
from aiva_gateway.optimize.citations import passage_header
from aiva_gateway.pipeline.interfaces import ExecutionPlan, InitialAssessment, RlcdPrediction, RouterRequest

_COMPLEXITY_LEVELS = ("simple", "moderate", "complex", "advanced")
_GROUNDED_IN = {"knowledge": "knowledge passages only", "records": "operational records"}


def grounding(passages: Iterable[str]) -> Grounding:
    """What an answer is grounded in, from its evidence passages' text. A knowledge passage (KB section or web
    result) opens with a citation header, the line the Context Guard uses; any passage without one is an
    operational record (incident or site record, alarm, tool output)."""
    kinds = {passage_header(text) is not None for text in passages}
    if not kinds:
        return "none"
    return "knowledge" if kinds == {True} else "records"


def request_costs(
    prices: PriceBook, models: Iterable[ModelSpec], *, input_tokens: int, max_output_tokens: int
) -> dict[str, int]:
    """Each model's estimated cost of serving one request, in micro-USD, for RLCD's comparison: its
    list-price tokens at the request's size and, for an in-house model, its GPU time at FinOps' per-attempt
    estimate (the reservation's). A $0 token rate no longer hides what the GPU costs, so everyday requests go
    to the cheapest API model and the sovereign model serves what policy sends it."""
    return {
        m.id: prices.worst_case_micros(
            m, input_tokens=input_tokens, max_output_tokens=max_output_tokens, attempts=1
        )
        + prices.gpu_worst_case_micros(m.id, attempts=1)
        for m in models
    }


class HeuristicRouter:
    version = "heuristic-v0"

    def assess(self, request: RouterRequest) -> InitialAssessment:
        task = request.task or request.scenario_task or ("rag" if request.evidence_count else "qa")
        tokens = request.original_context_tokens
        level = 0 if tokens < 500 else 1 if tokens < 2000 else 2 if tokens < 8000 else 3
        if request.quality_tier == "critical":
            level = min(level + 1, 3)
        complexity = _COMPLEXITY_LEVELS[level]
        sufficient = request.evidence_count == 0 and tokens < 1500
        local_fast = sufficient and complexity == "simple" and task in {"qa", "summarization"}
        reasons = [f"task={task}", f"complexity={complexity} ({tokens} estimated tokens)"]
        reasons.append("context sufficient without CIM" if sufficient else "context needs packaging")
        return InitialAssessment(
            rlcd_version=self.version,
            task_category=task,
            complexity=complexity,
            predicted_sensitivity=None,
            context_sufficient=sufficient,
            local_fast_eligible=local_fast,
            confidence=None,
            reasons=tuple(reasons),
        )

    def plan(
        self,
        request: RouterRequest,
        assessment: InitialAssessment,
        eligible: list[ModelSpec],
        prices: dict[str, int],
    ) -> ExecutionPlan:
        if not eligible:
            raise ValueError("RLCD requires at least one eligible model")
        floor = MIN_QUALITY_RANK[request.quality_tier]
        qualified = [m for m in eligible if m.quality_rank >= floor] or [
            max(eligible, key=lambda m: m.quality_rank)
        ]
        reasons = [f"quality tier {request.quality_tier} requires rank >= {floor}"]
        unqualified = [m.id for m in qualified if not m.answers(request.grounding)]
        if unqualified:
            grounded = [m for m in qualified if m.answers(request.grounding)]
            subject = f"answer grounded in {_GROUNDED_IN[request.grounding]}"
            if grounded:
                qualified = grounded
                reasons.append(f"{subject}: {', '.join(unqualified)} not qualified")
            else:
                # Never refuse for want of a better model: the eligible set is policy's, not RLCD's.
                reasons.append(f"{subject}: no other model qualifies, so {', '.join(unqualified)} kept")
        ranked = sorted(qualified, key=lambda m: (prices.get(m.id, 0), -m.quality_rank, m.id))
        chosen = ranked[0]
        preferred = next((m for m in qualified if m.id == request.preferred_model), None)
        if preferred is not None:
            chosen = preferred
            reasons.append(f"caller preference {preferred.id} honoured within the eligible set")
        else:
            reasons.append(f"lowest estimated cost per request among qualified models: {chosen.id}")
        fallbacks = tuple(m.id for m in ranked if m.id != chosen.id)
        return ExecutionPlan(
            rlcd_version=self.version,
            model_id=chosen.id,
            context_strategy="full" if assessment.context_sufficient else "evidence-preserving",
            validation_level="standard",
            confidence=None,
            reasons=tuple(reasons),
            fallback_model_ids=fallbacks,
        )


# Below these a head is `unclassified`; routing keeps the heuristic's value. Chosen by ML & Data for v1 and
# re-checked for v2 on its calibrated validation curves (ml/reports/rlcd-v2-gates-run1.md, realistic prior):
# coverage / accuracy at them is task 97.7% / 97.6%, complexity 75.8% / 97.0%, context need 99.4% / 98.6%.
MIN_CONFIDENCE = {"task_category": 0.60, "complexity": 0.80, "context_need": 0.60}
# At 0.5, v2 under-classifies 0.5% of requests with DLP alongside (1.6% alone) and over-classifies 0.3% to
# confidential or above (same report). Lower cut-offs gain at most 0.2 points, about four validation rows.
SENSITIVITY_TIGHTEN_AT = 0.50
SENSITIVITY_ORDER = ("public", "internal", "confidential", "restricted", "sovereign-only")


def render(messages: Iterable[dict[str, str]]) -> str:
    """The classifier's input: every message as `[role] content`, in order (aiva_ml.rlcd.labels.render)."""
    return "\n".join(f"[{m['role']}] {m['content']}" for m in messages)


def tightened_sensitivity(prediction: RlcdPrediction) -> Classification:
    """The highest level L with P(class >= L) >= 0.5. The caller only ever uses it to tighten."""
    labels, probs = prediction.labels["sensitivity"], prediction.probabilities["sensitivity"]
    by_label = dict(zip(labels, probs, strict=True))
    mass = 0.0
    for level in reversed(SENSITIVITY_ORDER):
        mass += by_label.get(level, 0.0)
        if mass >= SENSITIVITY_TIGHTEN_AT:
            return Classification(level)
    return Classification(SENSITIVITY_ORDER[0])


class ModelRouter(HeuristicRouter):
    """RLCD: routes on the classifier's predictions. Without one it is the heuristic, labelled so."""

    def assess(self, request: RouterRequest) -> InitialAssessment:
        fallback = super().assess(request)
        prediction = request.prediction
        if prediction is None:
            return replace(
                fallback, reasons=(*fallback.reasons, "RLCD model unavailable: heuristic assessment")
            )
        confidences = {head: prediction.top(head)[1] for head in prediction.probabilities}
        unclassified: list[str] = []
        reasons: list[str] = []

        hint = request.task or request.scenario_task
        task_label, task_confidence = prediction.top("task_category")
        if hint:
            task = hint
            reasons.append(f"task={task} (declared; model {task_label} at {task_confidence:.2f})")
        elif task_confidence >= MIN_CONFIDENCE["task_category"]:
            task = task_label
            reasons.append(f"task={task} ({task_confidence:.2f})")
        else:
            task = fallback.task_category
            unclassified.append("task_category")
            reasons.append(f"task unclassified ({task_label} at {task_confidence:.2f}); heuristic {task}")

        complexity_label, complexity_confidence = prediction.top("complexity")
        if complexity_confidence >= MIN_CONFIDENCE["complexity"]:
            complexity = complexity_label
            reasons.append(f"complexity={complexity} ({complexity_confidence:.2f})")
        else:
            # Unclassified complexity never escalates: routing keeps the heuristic's token-based value.
            complexity = fallback.complexity
            unclassified.append("complexity")
            reasons.append(f"complexity unclassified ({complexity_confidence:.2f}); heuristic {complexity}")

        need_label, need_confidence = prediction.top("context_need")
        if need_confidence >= MIN_CONFIDENCE["context_need"]:
            sufficient = need_label == "sufficient"
            reasons.append(f"context {need_label} ({need_confidence:.2f})")
        else:
            sufficient = fallback.context_sufficient
            unclassified.append("context_need")
            reasons.append(f"context need unclassified ({need_confidence:.2f}); heuristic")

        sensitivity = tightened_sensitivity(prediction)
        reasons.append(f"predicted sensitivity at least {sensitivity.value} (tighten-only)")
        local_fast = (
            sufficient
            and complexity == "simple"
            and "complexity" not in unclassified
            and task in {"qa", "summarization"}
        )
        return InitialAssessment(
            rlcd_version=prediction.model_version,
            task_category=task,
            complexity=complexity,
            predicted_sensitivity=sensitivity,
            context_sufficient=sufficient,
            local_fast_eligible=local_fast,
            confidence=None if hint else task_confidence,
            reasons=tuple(reasons),
            confidences=confidences,
            unclassified=tuple(unclassified),
        )

    def plan(
        self,
        request: RouterRequest,
        assessment: InitialAssessment,
        eligible: list[ModelSpec],
        prices: dict[str, int],
    ) -> ExecutionPlan:
        plan = super().plan(request, assessment, eligible, prices)
        return replace(plan, rlcd_version=assessment.rlcd_version, confidence=assessment.confidence)
