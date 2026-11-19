"""The Story mode's hero figures (/week, owner 2026-10-22) on the /live counters' definitions (ADR-0015).

`web/apps/control-center/src/live/counters.ts` and `quality.ts` sum each chat response into a session's
counters. A week of the same runs, read back from the run records, must give exactly the same figures.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from aiva_gateway.contracts.chat import ChatResponse
from aiva_gateway.contracts.reports import Amount, ReportFilters, ReportWindow
from aiva_gateway.core.enums import ExecutionPath, Outcome, Provenance
from aiva_gateway.db.models import AuditLogRow, ExecutionRunRow
from aiva_gateway.finops.frontier import direct_frontier_estimate
from aiva_gateway.finops.pricing import PriceBook
from aiva_gateway.optimize.cache import RedisResponseCache
from aiva_gateway.pipeline.interfaces import CachedResponse
from aiva_gateway.pipeline.response import build_chat_response
from aiva_gateway.reporting import traffic
from aiva_gateway.reporting.aggregate import load_run_facts
from aiva_gateway.reporting.lines import LedgerFact, RunFacts, SourceAnswer, run_lines
from aiva_gateway.reporting.queries import QUALITY_COMPARE, ReportContext, _quality_kpi, quality_kpi
from aiva_gateway.reporting.scope import ReportScope
from aiva_gateway.reporting.totals import Totals
from aiva_gateway.reporting.traffic import CORE_PLAN, SYNTHETIC_USERS, TrafficSpec, synthetic_pseudonym
from aiva_gateway.services import Services
from tests.insights.conftest import aggregator_for
from tests.insights.test_lines import CATALOG, DIMS, PRICING, attempt, charge, served_run

ANSWERED = dict(DIMS) | {"outcome": "success"}


def _lines(facts: RunFacts) -> dict[str, Any]:
    return run_lines(facts, PRICING)[0]


# --- One run's part ---------------------------------------------------------------------------------------


def test_an_answered_run_counts_its_route_spend_and_the_context_it_pruned() -> None:
    """CIM, a failed call and its retry are AiVa's route; the baseline comparison, an answer-quality check,
    a ledger correction and GPU hosting are not, as on /live. A billed web search is."""
    facts = served_run()
    facts.dimensions = dict(ANSWERED)
    facts.attempts.append(attempt("a-ref", "quality_reference", "claude-flagship"))
    facts.ledger += [
        charge("a-ref", 9_000, "quality_reference"),
        LedgerFact(None, "tool_call", 8_000, "measured", "rc-tavily-search"),
    ]
    run = _lines(facts)
    assert run["priced_request_count"] == 1
    assert run["priced_api_micros"] == 777 + 8_000  # the retry and the web search, never 8,000 + 9,000 - 50
    assert run["context_tokens_saved"] == 6_000 - 1_000
    assert run["optimizable_tokens"] == 6_000 + 200  # the original context and the served answer
    assert run["cache_tokens_saved"] == 0


def test_a_cache_hit_spares_the_tokens_it_avoided_at_no_route_cost() -> None:
    facts = RunFacts(
        "run-hit",
        dict(ANSWERED),
        "cache-return",
        "simulated",
        3.0,
        cache_hit=True,
        avoided_call=True,
        avoided_tokens=1_200,
        cache_source=SourceAnswer("gct-qwen-finetuned", 1_000, 200),
    )
    run = _lines(facts)
    assert (run["priced_request_count"], run["priced_api_micros"]) == (1, 0)
    assert run["optimizable_tokens"] == run["cache_tokens_saved"] == 1_200
    assert run["context_tokens_saved"] == 0
    assert run["frontier_baseline_micros"] == PRICING.frontier(1_000, 200)


def test_a_cache_hit_whose_source_is_gone_is_left_out_of_both_cost_sides() -> None:
    """Its baseline falls back to the source's own cost, not a frontier price (FinOps): never compared."""
    facts = RunFacts(
        "run-hit", dict(ANSWERED), "cache-return", "simulated", 3.0, cache_hit=True, avoided_tokens=1_200,
        avoided_micros=150,
    )  # fmt: skip
    run = _lines(facts)
    assert (run["priced_request_count"], run["priced_api_micros"], run["priced_frontier_micros"]) == (0, 0, 0)
    assert (
        run["cache_tokens_saved"] == run["optimizable_tokens"] == 1_200
    )  # the tokens it avoided still count


@pytest.mark.parametrize("outcome", ["quality_fail", "error"])
def test_a_run_that_was_not_answered_is_priced_but_spares_no_tokens(outcome: str) -> None:
    """/live counts a run's direct-frontier estimate whenever an answer was served, but only an answered
    (successful) run's context as optimized."""
    facts = served_run()
    facts.dimensions = dict(DIMS) | {"outcome": outcome}
    run = _lines(facts)
    assert run["priced_request_count"] == 1
    assert run["optimizable_tokens"] == run["context_tokens_saved"] == 0


def test_a_blocked_run_adds_nothing() -> None:
    facts = RunFacts("run-denied", dict(DIMS) | {"outcome": "policy_denied"}, "none", "measured", 1.0)
    run = _lines(facts)
    assert all(
        run[name] == 0
        for name in (
            "priced_request_count",
            "priced_api_micros",
            "priced_frontier_micros",
            "frontier_baseline_micros",
            "optimizable_tokens",
            "context_tokens_saved",
            "cache_tokens_saved",
        )
    )


def test_the_direct_frontier_side_is_finops_estimate() -> None:
    """The run line's frontier baseline is `finops.frontier.direct_frontier_estimate` of the same run, for a
    served answer and for a cache hit (priced on the tokens the cache entry models)."""
    prices = PriceBook(CATALOG)
    facts = served_run()
    served = facts.attempts[2]  # the successful retry
    direct, label = direct_frontier_estimate(
        prices,
        served=SimpleNamespace(
            input_tokens=served.input_tokens,
            output_tokens=served.output_tokens,
            provenance=Provenance.MEASURED,
        ),  # type: ignore[arg-type]
        context_before=facts.context_tokens_before,
        context_after=facts.context_tokens_after,
    )
    assert _lines(facts)["frontier_baseline_micros"] == direct and label is Provenance.ESTIMATED
    hit = CachedResponse(
        source_run_id="run-src",
        model_id="gct-qwen-finetuned",
        content="",
        modeled_input_tokens=1_000,
        modeled_output_tokens=200,
        modeled_cost_micros=0,
        provenance=Provenance.SIMULATED,
    )
    direct, _ = direct_frontier_estimate(
        prices, served=None, context_before=0, context_after=0, cache_hit=hit
    )
    facts = RunFacts(
        "run-hit", dict(ANSWERED), "cache-return", "simulated", 1.0, cache_hit=True,
        cache_source=SourceAnswer("gct-qwen-finetuned", 1_000, 200),
    )  # fmt: skip
    assert _lines(facts)["frontier_baseline_micros"] == direct


# --- The hero figures ---------------------------------------------------------------------------------------


def _week() -> Totals:
    answered = served_run()
    answered.dimensions = dict(ANSWERED)
    hit = RunFacts(
        "run-hit", dict(ANSWERED), "cache-return", "measured", 1.0, cache_hit=True, avoided_tokens=1_200,
        cache_source=SourceAnswer("gct-qwen-finetuned", 1_000, 200),
    )  # fmt: skip
    denied = RunFacts("run-denied", dict(DIMS) | {"outcome": "policy_denied"}, "none", "measured", 1.0)
    held = RunFacts("run-held", dict(DIMS) | {"outcome": "approval_required"}, "none", "measured", 1.0)
    sovereign = RunFacts(
        "run-sov",
        dict(ANSWERED) | {"sensitivity": "confidential", "execution_path": "sovereign-direct"},
        "sovereign-direct",
        "measured",
        1.0,
    )
    sovereign.attempts = [attempt("s1", "upstream", "gct-qwen-finetuned", tokens=(500, 100))]
    sovereign.ledger = [charge("s1", 0), charge("s1", 400, "gpu_estimate")]
    sovereign.context_tokens_before = sovereign.context_tokens_after = 500
    totals = Totals()
    for facts in (answered, hit, denied, held, sovereign):
        totals.extend(run_lines(facts, PRICING))
    return totals


def test_cost_saved_compares_like_with_like_and_names_the_hosting_it_leaves_out() -> None:
    totals = _week()
    cost = totals.cost_kpi()
    assert cost.priced_requests == 3  # answered, the cache hit and the sovereign answer; never the blocked
    assert cost.aiva.value == 777 and cost.aiva.provenance is Provenance.MEASURED
    assert cost.direct_frontier.value == totals.m("frontier_baseline_micros") > 0  # every hit has a source
    assert cost.direct_frontier.provenance is Provenance.ESTIMATED
    assert (cost.saved.numerator, cost.saved.denominator) == (
        cost.direct_frontier.value - 777,
        cost.direct_frontier.value,
    )
    assert cost.hosting_excluded.value == 900 + 400 and cost.provenance is Provenance.ESTIMATED
    # The waterfall's frontier baseline is the same direct-frontier figure; its net adds GPU and corrections.
    steps = {s.step: s.amount_micros for s in totals.savings("claude-flagship", "v").steps}
    assert steps["frontier_baseline"] == cost.direct_frontier.value


def test_tokens_fewer_splits_caching_and_context() -> None:
    tokens = _week().tokens_kpi()
    assert (tokens.caching_tokens, tokens.context_tokens) == (1_200, 5_000)
    assert tokens.base_tokens == 6_200 + 1_200 + (500 + 100)
    assert (tokens.fewer.numerator, tokens.fewer.denominator) == (6_200, tokens.base_tokens)
    assert tokens.context_provenance is Provenance.ESTIMATED  # token estimates, never measured


def test_governance_counts_the_stopped_and_the_sensitive_kept_sovereign() -> None:
    governance = _week().governance_kpi()
    assert (governance.requests, governance.stopped_by_policy) == (5, 2)
    assert (governance.sensitive_requests, governance.sensitive_on_sovereign) == (1, 1)


def test_synthetic_traffic_is_never_labelled_better_than_synthetic() -> None:
    facts = served_run()
    facts.dimensions = dict(ANSWERED) | {"data_designation": "synthetic"}
    totals = Totals().extend(row | {"data_designation": "synthetic"} for row in run_lines(facts, PRICING))
    kpis = (totals.cost_kpi(), totals.tokens_kpi(), totals.governance_kpi())
    assert {k.provenance for k in kpis} == {Provenance.SYNTHETIC}


def _audit(status: str, score: int | None = None) -> dict[str, Any]:
    return {"status": status, "score": score}


def test_quality_is_one_latest_comparison_per_run_and_never_fills_in_a_mean() -> None:
    cost = Amount(value=9_300, provenance=Provenance.MEASURED)
    kpi = quality_kpi(
        [
            ("run-a", "actual", _audit("compared", 60)),
            ("run-a", "actual", _audit("compared", 90)),  # a repeated check replaces the run's entry
            ("run-a", "actual", _audit("denied")),  # a denied request never does
            ("run-b", "actual", _audit("failed")),  # called, but no score
            ("run-c", "actual", _audit("compared", 70)),
            ("run-d", "actual", _audit("not_reserved")),
        ],
        cost,
    )
    assert (kpi.compared_requests, kpi.mean_score, kpi.cost, kpi.provenance) == (
        2,
        80.0,
        cost,
        Provenance.ESTIMATED,
    )
    empty = quality_kpi([], cost)
    assert (empty.compared_requests, empty.mean_score) == (0, None)
    synthetic = quality_kpi([("run-a", "synthetic", _audit("compared", 50))], cost)
    assert synthetic.provenance is Provenance.SYNTHETIC


# --- Against the chat responses themselves --------------------------------------------------------------


def _live(response: ChatResponse) -> dict[str, int]:
    """`live/counters.ts` runFigures, for the cost and token counters."""
    cache_hit = response.cache.hit and response.execution_path is ExecutionPath.CACHE_RETURN
    answered = response.outcome is Outcome.SUCCESS and not cache_hit
    usage, cost, cache = response.usage, response.cost, response.cache
    original = usage.original_context_tokens or 0
    pruned = max(0, original - (usage.constructed_context_tokens or 0)) if answered else 0
    avoided = (cache.avoided_input_tokens or 0) + (cache.avoided_output_tokens or 0) if cache_hit else 0
    api = (cost.upstream_micros or 0) + (cost.cim_micros or 0) + (cost.tool_micros or 0)
    direct = cost.direct_frontier_estimated_micros
    base = avoided if cache_hit else original + (usage.upstream_output_tokens or 0) if answered else 0
    return {
        "priced_request_count": int(direct is not None),
        "priced_frontier_micros": direct or 0,
        "priced_api_micros": api if direct is not None else 0,
        "cache_tokens_saved": avoided,
        "context_tokens_saved": pruned,
        "optimizable_tokens": base,
    }


@pytest.fixture
def responses(monkeypatch: pytest.MonkeyPatch, services: Services) -> Iterator[list[ChatResponse]]:
    """The chat response of every run the traffic generator persists, built as the API builds it."""
    captured: list[ChatResponse] = []
    persist = traffic.persist_run

    async def persist_and_answer(session: Any, catalog: Any, ctx: Any, spans: Any) -> None:
        await persist(session, catalog, ctx, spans)
        captured.append(build_chat_response(ctx, spans, prices=services.prices))

    monkeypatch.setattr(traffic, "persist_run", persist_and_answer)
    yield captured


async def test_a_week_of_runs_sums_exactly_what_live_counts_for_them(services, responses) -> None:  # type: ignore[no-untyped-def]
    # The core plan has a full cache hit, which needs a response cache (the shared test settings have none).
    cache = RedisResponseCache(services.redis, embedder=None, namespace=f"aiva:cache:value-{uuid4().hex}")
    await traffic.generate_traffic(replace(services, cache=cache), plan=list(CORE_PLAN), tenant_id="gct")
    live = [_live(response) for response in responses]
    # Simulated CIM keeps every passage, so pruning is covered by the unit tests above; a cache hit is here.
    assert any(r["cache_tokens_saved"] for r in live) and any(r["optimizable_tokens"] for r in live)
    assert any(not r["priced_request_count"] for r in live)  # the plan's policy denial
    aggregator = aggregator_for(services)
    async with services.sessionmaker() as session:
        facts = await load_run_facts(
            session, aggregator.threshold, ExecutionRunRow.run_id.in_([r.run_id for r in responses])
        )
    for response, expected in zip(responses, live, strict=True):
        run = run_lines(facts[response.run_id], aggregator.pricing)[0]
        assert {name: run[name] for name in expected} == expected, response.run_id


async def test_quality_reads_the_comparisons_of_the_runs_in_scope(services) -> None:  # type: ignore[no-untyped-def]
    """The audit rows of three runs of one synthetic user, filtered to that user; rolled back."""
    user = next(u for u in SYNTHETIC_USERS if u.application_id == "gct-noc-assistant")
    spec = TrafficSpec(user, "Correlate the synthetic alarms from cell site RUH-0412.", label="quality-kpi")
    report = await traffic.generate_traffic(services, plan=[spec, spec, spec], tenant_id="gct")
    run_a, run_b, run_c = report.by_label["quality-kpi"]
    today = datetime.now(UTC).date()
    ctx = ReportContext(
        scope=ReportScope(tenant_id="gct", tenant_wide=True, department_id=None, application_ids=frozenset()),
        window=ReportWindow.model_validate({"from": today, "to": today}),
        filters=ReportFilters(user=synthetic_pseudonym(services, user)),
        catalog=services.catalog,
        pricing_snapshot=services.prices.version,
    )
    async with services.sessionmaker() as session, session.begin():
        for run_id, details in [
            (run_a, _audit("compared", 40)),
            (run_a, _audit("compared", 90)),
            (run_b, _audit("failed")),
            (run_c, _audit("compared", 70)),
            (run_c, _audit("denied")),
        ]:
            session.add(
                AuditLogRow(
                    tenant_id="gct",
                    actor_kind="user",
                    actor_id="usr_presenter",
                    action=QUALITY_COMPARE,
                    target_type="run",
                    target_id=run_id,
                    details=details,
                )
            )
        await session.flush()
        kpi = await _quality_kpi(session, ctx, Totals())
        await session.rollback()
    assert (kpi.compared_requests, kpi.mean_score) == (2, 80.0)
    assert kpi.provenance is Provenance.SYNTHETIC  # synthetic traffic
