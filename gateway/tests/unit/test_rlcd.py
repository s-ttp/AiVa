from pathlib import Path

from aiva_gateway.catalog import load_catalog
from aiva_gateway.catalog.models import Grounding, ModelSpec
from aiva_gateway.config import REPO_ROOT
from aiva_gateway.finops.pricing import PriceBook
from aiva_gateway.optimize.knowledge import evidence_text
from aiva_gateway.optimize.rlcd import HeuristicRouter, grounding, request_costs
from aiva_gateway.pipeline.interfaces import ExecutionPlan, RouterRequest

catalog = load_catalog(Path(REPO_ROOT, "config"))
router = HeuristicRouter()
ALL = [m for m in catalog.models.models if m.role == "upstream"]
PRICES = {
    "gct-qwen-finetuned": 0,
    "gemini-flash-lite": 2_800_000,
    "deepseek-v4-1-flash": 800_000,
    "claude-flagship": 24_000_000,
}


def request(**overrides: object) -> RouterRequest:
    base: dict[str, object] = {
        "task": None,
        "scenario_task": None,
        "quality_tier": "standard",
        "original_context_tokens": 100,
        "evidence_count": 0,
        "preferred_model": None,
        "routing_mode": "hybrid",
    }
    base.update(overrides)
    return RouterRequest(**base)  # type: ignore[arg-type]


def test_short_qa_is_local_fast_eligible() -> None:
    assessment = router.assess(request())
    assert assessment.local_fast_eligible and assessment.context_sufficient


def test_evidence_requires_context_packaging() -> None:
    assessment = router.assess(request(evidence_count=2))
    assert not assessment.context_sufficient and assessment.task_category == "rag"


def test_plan_stays_inside_eligible_set() -> None:
    eligible = [m for m in ALL if m.id == "gct-qwen-finetuned"]
    plan = router.plan(request(preferred_model="claude-flagship"), router.assess(request()), eligible, PRICES)
    assert plan.model_id == "gct-qwen-finetuned"


def test_quality_tier_sets_minimum_rank() -> None:
    plan = router.plan(request(quality_tier="critical"), router.assess(request()), ALL, PRICES)
    assert plan.model_id == "claude-flagship"


def test_preference_honoured_when_eligible_and_qualified() -> None:
    plan = router.plan(request(preferred_model="deepseek-v4-1-flash"), router.assess(request()), ALL, PRICES)
    assert plan.model_id == "deepseek-v4-1-flash"


# Routing by real cost (ADR-0009, 2026-10-22 amendment): an in-house model's GPU time counts, so a $0 token
# rate no longer wins everyday requests.
BOOK = PriceBook(catalog)


def test_an_in_house_models_cost_includes_its_gpu_time() -> None:
    costs = request_costs(BOOK, ALL, input_tokens=6_000, max_output_tokens=1_024)
    qwen = next(m for m in ALL if m.id == "gct-qwen-finetuned")
    assert BOOK.price(qwen.id).input_micros_per_mtok == 0  # $0 tokens ...
    assert costs[qwen.id] == BOOK.gpu_worst_case_micros(qwen.id, attempts=1) > 0  # ... but not free
    gemini = next(m for m in ALL if m.id == "gemini-flash-lite")
    assert costs[gemini.id] == BOOK.worst_case_micros(
        gemini, input_tokens=6_000, max_output_tokens=1_024, attempts=1
    )


def test_everyday_requests_go_to_the_cheapest_api_model_and_the_sovereign_set_keeps_its_model() -> None:
    costs = request_costs(BOOK, ALL, input_tokens=6_000, max_output_tokens=1_024)
    standard = router.plan(request(evidence_count=8), router.assess(request(evidence_count=8)), ALL, costs)
    assert standard.model_id == "gemini-flash-lite"
    assert "lowest estimated cost per request among qualified models: gemini-flash-lite" in standard.reasons
    high = router.plan(request(quality_tier="high"), router.assess(request()), ALL, costs)
    assert high.model_id == "deepseek-v4-1-flash"
    # The sovereign direct path offers only the sovereign model: price never moves it, and it has no fallback.
    sovereign = [m for m in ALL if m.sovereign]
    only = router.plan(
        request(),
        router.assess(request()),
        sovereign,
        request_costs(BOOK, sovereign, input_tokens=6_000, max_output_tokens=1_024),
    )
    assert only.model_id == "gct-qwen-finetuned" and only.fallback_model_ids == ()


# Grounded answers (ADR-0009, 2026-10-22 grounded-answers amendment): on knowledge passages only, Gemini does
# not qualify at the standard tier; on operational records, and ungrounded, it stays the cheapest qualified
# model.
KB = evidence_text(
    "GCT-POL-014", 2, "Enterprise Roaming Bundle Discount Approval (GCT Group)", "Approval tiers", "x"
)
WEB = "[WEB-1 §1] Roaming day passes - example.com\nSource: https://example.com/roaming\nAED 35 a day."
RECORD = "INC-7781 opened 06:42 UTC: degraded 4G data in Riyadh North."


def test_grounding_is_read_from_the_citation_header() -> None:
    assert grounding([]) == "none"
    assert grounding([KB, WEB]) == "knowledge"
    assert grounding([KB, RECORD]) == grounding([RECORD]) == "records"
    # The header must open the passage: a citation quoted inside a record does not make it knowledge.
    assert grounding([f"Note: see {KB.splitlines()[0]}"]) == "records"


def _standard(kind: str, eligible: list[ModelSpec] | None = None, **overrides: object) -> ExecutionPlan:
    costs = request_costs(BOOK, eligible or ALL, input_tokens=6_000, max_output_tokens=1_024)
    req = request(evidence_count=8, grounding=kind, **overrides)
    return router.plan(req, router.assess(req), eligible or ALL, costs)


def test_knowledge_grounded_standard_answers_go_to_the_cheapest_qualified_model() -> None:
    plan = _standard("knowledge")
    assert plan.model_id == "deepseek-v4-1-flash"
    assert "answer grounded in knowledge passages only: gemini-flash-lite not qualified" in plan.reasons
    assert "gemini-flash-lite" not in plan.fallback_model_ids
    # A caller preference never overrides qualification, as with the quality tier.
    assert _standard("knowledge", preferred_model="gemini-flash-lite").model_id == "deepseek-v4-1-flash"


def test_record_grounded_and_ungrounded_standard_answers_keep_gemini() -> None:
    for kind in ("records", "none"):
        plan = _standard(kind)
        assert plan.model_id == "gemini-flash-lite", kind
        assert not any("not qualified" in r for r in plan.reasons), kind


def test_an_unqualified_model_still_serves_when_nothing_else_qualifies() -> None:
    gemini = [m for m in ALL if m.id == "gemini-flash-lite"]
    plan = _standard("knowledge", gemini)
    assert plan.model_id == "gemini-flash-lite"
    assert (
        "answer grounded in knowledge passages only: no other model qualifies, so gemini-flash-lite kept"
        in (plan.reasons)
    )


def test_grounding_never_widens_the_eligible_set_or_moves_the_sovereign_path() -> None:
    no_deepseek = [m for m in ALL if m.id in {"gemini-flash-lite", "claude-flagship"}]
    assert _standard("knowledge", no_deepseek).model_id == "claude-flagship"  # eligible, qualified, dearer
    sovereign = [m for m in ALL if m.sovereign]
    for kind in ("none", "knowledge", "records"):
        assert _standard(kind, sovereign).model_id == "gct-qwen-finetuned"


def test_answer_temperatures_apply_to_grounded_answers_only() -> None:
    # (none, knowledge, records); None is the provider's default. Claude's thinking takes no temperature.
    expected = {
        "deepseek-v4-1-flash": (None, 0.0, 0.0),
        "gemini-flash-lite": (None, None, 0.3),
        "gct-qwen-finetuned": (None, None, None),
        "claude-flagship": (None, None, None),
    }
    kinds: tuple[Grounding, ...] = ("none", "knowledge", "records")
    for m in ALL:
        assert tuple(m.answer_temperature(kind) for kind in kinds) == expected[m.id], m.id
