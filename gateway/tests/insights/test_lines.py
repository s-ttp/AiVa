"""Line building and the savings identity, without a database (ADR-0015)."""

from aiva_gateway.catalog import load_catalog
from aiva_gateway.config import REPO_ROOT
from aiva_gateway.finops.pricing import PriceBook
from aiva_gateway.reporting.aggregate import build_pricing
from aiva_gateway.reporting.dimensions import DIMENSIONS, RUN_DIMENSIONS
from aiva_gateway.reporting.lines import (
    MAX_TOKENS_PER_ESTIMATE,
    AttemptFact,
    LedgerFact,
    RunFacts,
    SourceAnswer,
    line_key,
    metered_original,
    quality_exception_run,
    run_lines,
)
from aiva_gateway.reporting.totals import Totals

CATALOG = load_catalog(REPO_ROOT / "config")
PRICING = build_pricing(CATALOG, PriceBook(CATALOG))
DIMS = dict.fromkeys(RUN_DIMENSIONS, "x") | {"sensitivity": "internal"}


def attempt(
    attempt_id: str,
    stage: str,
    model: str,
    *,
    ok: bool = True,
    retry_of: str | None = None,
    tokens: tuple[int, int] = (1000, 200),
    cached: int = 0,
) -> AttemptFact:
    return AttemptFact(
        attempt_id,
        stage,
        model,
        "provider",
        "ok" if ok else "error",
        retry_of,
        tokens[0] if ok else 0,
        tokens[1] if ok else 0,
        cached,
        0,
        12.5,
        "measured",
    )


def charge(attempt_id: str | None, amount: int, kind: str = "upstream_inference") -> LedgerFact:
    return LedgerFact(attempt_id, kind, amount, "measured", "rc-test")


def served_run() -> RunFacts:
    """CIM call, a failed upstream call, its successful retry, a baseline call and a later correction."""
    facts = RunFacts("run-1", dict(DIMS), "cim-assisted", "measured", 250.0)
    facts.attempts = [
        attempt("a-cim", "cim", "enterprise-cim", tokens=(3000, 400)),
        attempt("a-up", "upstream", "gemini-flash-lite", ok=False),
        attempt("a-retry", "upstream", "gemini-flash-lite", retry_of="a-up", cached=100),
        attempt("a-base", "baseline", "claude-flagship"),
    ]
    facts.ledger = [
        charge("a-cim", 0, "cim"),
        charge("a-cim", 900, "gpu_estimate"),
        charge("a-up", 0),
        charge("a-retry", 777, "retry"),
        charge("a-base", 8000, "baseline_comparison"),
        charge("a-retry", -50, "adjustment"),
    ]
    facts.context_tokens_before, facts.context_tokens_after = 6000, 1000
    facts.quality_passed = True
    return facts


def test_every_ledger_charge_lands_in_exactly_one_line() -> None:
    lines = run_lines(served_run(), PRICING)
    assert [line["line_kind"] for line in lines] == [
        "run",
        "attempt",
        "attempt",
        "attempt",
        "attempt",
        "adjustment",
    ]
    assert sum(line["api_micros"] for line in lines) == 777 + 8000 - 50
    assert sum(line["gpu_estimated_micros"] for line in lines) == 900
    assert sum(line["request_count"] for line in lines) == 1
    assert sum(line["retry_count"] for line in lines) == 1
    assert all(len(line_key(line)) == 32 and all(name in line for name in DIMENSIONS) for line in lines)


def test_savings_steps_sum_to_actual_spend_plus_gpu() -> None:
    totals = Totals().extend(run_lines(served_run(), PRICING))
    waterfall = totals.savings("claude-flagship", PRICING.version)
    steps = {s.step: s.amount_micros for s in waterfall.steps}
    assert sum(v for k, v in steps.items() if k != "net") == steps["net"]
    spend = totals.spend()
    # Every ledger amount appears in exactly one step, or as the measurement cost after the net: the opt-in
    # baseline comparison never reduces the savings.
    assert "baseline_comparison" not in steps
    assert waterfall.measurement_cost.value == spend.baseline_comparison_micros == 8000
    assert steps["net"] + waterfall.measurement_cost.value == spend.api.total + spend.gpu_estimated.value
    assert waterfall.net_savings_micros == steps["frontier_baseline"] - steps["net"]
    assert steps["context"] < 0  # 5,000 fewer tokens sent than the original context
    assert (
        spend.retry_micros == 777
        and spend.adjustment_micros == -50
        and spend.baseline_comparison_micros == 8000
    )
    baseline = totals.baseline("claude-flagship", "Frontier")
    assert baseline.runs == baseline.quality_passed_runs == 1
    assert baseline.savings.value == 8000 - (0 + 0 + 777)


def _served_alone(model: str, *, metered: int, before: int, after: int, cost: int) -> RunFacts:
    """A run with one upstream attempt whose provider counted `metered` input tokens, while the gateway
    estimated the original and constructed context at `before` and `after` tokens."""
    facts = RunFacts("run-basis", dict(DIMS), "cim-assisted", "measured", 10.0)
    facts.attempts = [attempt("a-up", "upstream", model, tokens=(metered, 300))]
    facts.ledger = [charge("a-up", cost)]
    facts.context_tokens_before, facts.context_tokens_after = before, after
    return facts


def test_the_frontier_model_serving_a_run_saves_nothing_by_routing() -> None:
    """The provider counts twice the gateway's estimate (system prompt, tokenizer): routing is still 0."""
    listed = PRICING.list_price("claude-flagship", 2000, 300)
    [run, _] = run_lines(
        _served_alone("claude-flagship", metered=2000, before=1000, after=1000, cost=listed), PRICING
    )
    assert run["savings_routing_micros"] == 0
    assert run["savings_context_micros"] == 0 and run["savings_prompt_cache_micros"] == 0
    assert run["frontier_baseline_micros"] == listed


def test_every_step_is_priced_on_the_served_attempts_metered_tokens() -> None:
    """Estimated 6,000 → 1,000 tokens, metered 2,000: the original context is 12,000 on the metered basis."""
    facts = _served_alone("gemini-flash-lite", metered=2000, before=6000, after=1000, cost=150)
    [run, _] = run_lines(facts, PRICING)
    frontier_sent = PRICING.frontier(2000, 300)
    assert run["frontier_baseline_micros"] == PRICING.frontier(12000, 300)
    assert run["savings_context_micros"] == PRICING.frontier(12000, 300) - frontier_sent
    assert (
        run["savings_routing_micros"]
        == frontier_sent - PRICING.list_price("gemini-flash-lite", 2000, 300)
        > 0
    )
    steps = ("savings_context_micros", "savings_routing_micros", "savings_prompt_cache_micros")
    assert run["frontier_baseline_micros"] - sum(run[step] for step in steps) == 150


def test_the_original_context_on_the_metered_basis() -> None:
    assert metered_original(2000, before=6000, after=1000) == 12000  # a tokenizer ratio of 2 scales
    assert metered_original(1326, before=641, after=641) == 1326  # nothing removed: the metered count
    assert metered_original(900, before=0, after=0) == 900  # no context recorded: the metered count
    assert metered_original(500, before=100, after=1000) == 50  # retrieval grew the context tenfold
    # A tiny template-only context: 200 metered for 10 estimated is overhead, not a tokenizer ratio of 20, so
    # the 990 removed tokens are priced at the cap of 3 per estimated token, not 20 (which would give 20,000).
    assert metered_original(200, before=1000, after=10) == 200 + 990 * MAX_TOKENS_PER_ESTIMATE


def test_a_run_without_a_recorded_context_prices_the_baseline_on_the_metered_count() -> None:
    [run, _] = run_lines(_served_alone("gemini-flash-lite", metered=900, before=0, after=0, cost=60), PRICING)
    assert run["frontier_baseline_micros"] == PRICING.frontier(900, 300)
    assert run["savings_context_micros"] == 0


def test_a_billed_web_search_is_its_own_overhead_not_a_correction() -> None:
    """A tool_call ledger row (no attempt) is a cost of serving: its own spend field and waterfall step."""
    facts = _served_alone("gemini-flash-lite", metered=900, before=900, after=900, cost=60)
    facts.ledger.append(LedgerFact(None, "tool_call", 8000, "measured", "rc-tavily-search"))
    totals = Totals().extend(run_lines(facts, PRICING))
    spend = totals.spend()
    assert spend.tool_call_micros == 8000 and spend.other_micros == 0 and spend.adjustment_micros == 0
    waterfall = totals.savings("claude-flagship", "v")
    steps = {s.step: s for s in waterfall.steps}
    assert steps["tool_calls"].amount_micros == 8000 and steps["adjustments"].amount_micros == 0
    assert steps["tool_calls"].provenance.value == "measured"
    assert sum(s.amount_micros for name, s in steps.items() if name != "net") == steps["net"].amount_micros
    assert steps["net"].amount_micros == spend.api.total + spend.gpu_estimated.value == 60 + 8000


def _compared(facts: RunFacts) -> RunFacts:
    """Add a presenter's answer-quality comparison (ADR-0023): a frontier reference and a grader call, each
    with its own `quality_*` ledger row on the run."""
    facts.attempts += [
        attempt("a-ref", "quality_reference", "claude-flagship", tokens=(4000, 600)),
        attempt("a-grade", "quality_grading", "gemini-flash-lite", tokens=(5000, 150)),
    ]
    facts.ledger += [charge("a-ref", 28_000, "quality_reference"), charge("a-grade", 700, "quality_grading")]
    return facts


def test_an_answer_quality_comparison_is_a_measurement_cost_never_a_saving() -> None:
    plain = Totals().extend(run_lines(served_run(), PRICING))
    totals = Totals().extend(run_lines(_compared(served_run()), PRICING))
    waterfall, before = totals.savings("claude-flagship", "v"), plain.savings("claude-flagship", "v")
    steps = {s.step: s.amount_micros for s in waterfall.steps}
    # Not a cost of serving: the waterfall is the run's own, and nothing lands in the corrections.
    assert steps == {s.step: s.amount_micros for s in before.steps}
    assert steps["adjustments"] == -50 and waterfall.net_savings_micros == before.net_savings_micros
    spend = totals.spend()
    assert spend.quality_comparison_micros == 28_700 and spend.other_micros == 0
    assert waterfall.measurement_cost.value == spend.baseline_comparison_micros + 28_700 == 8000 + 28_700
    assert waterfall.measurement_cost.provenance.value == "measured"
    assert steps["net"] + waterfall.measurement_cost.value == spend.api.total + spend.gpu_estimated.value
    tokens = totals.tokens()
    assert (tokens.quality_comparison_input_tokens, tokens.quality_comparison_output_tokens) == (9000, 750)
    assert tokens.other_input_tokens == tokens.other_output_tokens == 0
    # The baseline comparison still compares the frontier with what serving the run cost, comparison aside.
    assert totals.baseline("claude-flagship", "Frontier").actual.value == 777
    assert plain.spend().quality_comparison_micros == 0


def _sovereign(*, exception: bool, allowed: set[str] | None = None) -> RunFacts:
    """A confidential run served on the sovereign model, then compared (ADR-0023). By default both comparison
    calls were allowed by their own policy events."""
    facts = RunFacts(
        "run-s11",
        dict(DIMS) | {"sensitivity": "confidential"},
        "sovereign-direct",
        "measured",
        1.0,
        quality_exception=exception,
        allowed_comparisons=allowed if allowed is not None else {"a-ref", "a-grade"},
    )
    facts.attempts = [attempt("a1", "upstream", "gct-qwen-finetuned")]
    facts.ledger = [charge("a1", 0), charge("a1", 400, "gpu_estimate")]
    return _compared(facts)


def test_a_comparison_under_the_sovereign_exception_is_counted_apart_from_the_served_path() -> None:
    """Scenario 11 (ADR-0023 §5): the answer stays on the sovereign model and the comparison calls leave."""
    lines = run_lines(_sovereign(exception=True), PRICING)
    assert lines[0]["confidential_external_attempts"] == 0 and lines[0]["confidential_cim_attempts"] == 0
    totals = Totals().extend(lines)
    assert totals.m("confidential_external_attempts") == 0
    assert totals.m("confidential_comparison_attempts") == 2  # reported, never hidden
    assert totals.savings("claude-flagship", "v").measurement_cost.value == 28_700


def test_a_comparison_outside_the_exception_counts_as_a_leak() -> None:
    """Policy should make it impossible; if a comparison ever leaves another confidential run, the report
    shows a leak, so the exception can never mask a breach."""
    [run, *_] = run_lines(_sovereign(exception=False), PRICING)
    assert run["confidential_external_attempts"] == 2 and run["confidential_comparison_attempts"] == 0


def test_a_comparison_its_own_policy_did_not_allow_counts_as_a_leak() -> None:
    """The run record cannot show the synthetic sample or the presenter's request: only the call's own
    policy events can, so a call they did not allow is a leak even on a run that meets the record's gates."""
    [run, *_] = run_lines(_sovereign(exception=True, allowed={"a-ref"}), PRICING)
    assert run["confidential_external_attempts"] == 1 and run["confidential_comparison_attempts"] == 1


def test_a_comparison_call_leaves_whatever_the_catalog_says_of_its_model() -> None:
    """A grader the catalog does not list (unpinned since, or a stand-in) still counts as an egress call."""
    facts = _sovereign(exception=False)
    facts.attempts[-1] = attempt("a-grade", "quality_grading", "unlisted-grader", tokens=(5000, 150))
    [run, *_] = run_lines(facts, PRICING)
    assert run["confidential_external_attempts"] == 2


def test_the_exception_needs_every_gate_its_run_record_holds() -> None:
    gates = {
        "tenant_id": "gct",
        "scenario_id": "live-sovereign-routing",
        "classification": "confidential",
        "routing_mode": "hybrid",
        "data_designation": "synthetic",
    }
    assert quality_exception_run(**gates)
    for name, value in [
        ("tenant_id", "other"),
        ("scenario_id", "live-frontier-optimization"),
        ("scenario_id", None),
        ("classification", "restricted"),
        ("classification", "sovereign-only"),
        ("routing_mode", "local-only"),
        ("data_designation", "production"),
    ]:
        assert not quality_exception_run(**(gates | {name: value})), name


def test_cache_hit_avoids_the_source_answers_frontier_cost() -> None:
    facts = RunFacts(
        "run-2",
        dict(DIMS),
        "cache-return",
        "simulated",
        3.0,
        cache_hit=True,
        avoided_call=True,
        avoided_tokens=1200,
        avoided_micros=0,
        cache_source=SourceAnswer("gct-qwen-finetuned", 1000, 200),
    )
    [line] = run_lines(facts, PRICING)
    frontier = PRICING.frontier(1000, 200)
    assert line["frontier_baseline_micros"] == line["savings_cache_micros"] == frontier > 0
    totals = Totals().extend([line])
    assert totals.avoided().calls == 1 and totals.avoided().provenance.value == "simulated"
    waterfall = totals.savings("claude-flagship", "v")
    assert {s.step: s.amount_micros for s in waterfall.steps}["net"] == 0
    # No comparison call: the zero carries the run's label, never `measured` by default.
    assert waterfall.measurement_cost.value == 0
    assert waterfall.measurement_cost.provenance.value == "simulated"
    assert totals.baseline("claude-flagship", "Frontier").baseline.provenance.value == "simulated"


def test_confidential_run_counts_external_and_cim_attempts() -> None:
    facts = RunFacts(
        "run-3", dict(DIMS) | {"sensitivity": "confidential"}, "sovereign-direct", "measured", 1.0
    )
    facts.attempts = [attempt("a1", "upstream", "gct-qwen-finetuned")]
    [run, _] = run_lines(facts, PRICING)
    assert run["confidential_external_attempts"] == 0 and run["confidential_cim_attempts"] == 0
