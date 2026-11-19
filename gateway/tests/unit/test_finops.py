"""FinOps arithmetic: periods, buckets, allocations, envelopes, thresholds and lower-cost narrowing."""

from datetime import UTC, datetime

import pytest

from aiva_gateway.catalog import load_catalog
from aiva_gateway.config import REPO_ROOT
from aiva_gateway.core.enums import Provenance
from aiva_gateway.finops.budgets import BudgetDef, budget_id, forecast_micros, make_book, to_bps
from aiva_gateway.finops.envelope import build_envelope, narrow_for_lower_cost, upstream_costs
from aiva_gateway.finops.pricing import PriceBook
from aiva_gateway.finops.scopes import (
    ILLUSTRATIVE,
    INTERNET_GPU,
    LIVE,
    Bucket,
    allocate,
    bucket_expiry,
    commit,
    least_trusted,
    period_bounds,
    period_key,
    run_domain,
    scope_chain,
    visitor_domain,
)
from aiva_gateway.pipeline.interfaces import Charge, QuotaDeniedError, ReservationLine

CATALOG = load_catalog(REPO_ROOT / "config")
PRICES = PriceBook(CATALOG)
AT = datetime(2026, 12, 31, 23, 30, tzinfo=UTC)


def _budget(**overrides: object) -> BudgetDef:
    values: dict[str, object] = {
        "id": "bud-x",
        "tenant_id": "gct",
        "scope_type": "application",
        "scope_id": "app",
        "period": "monthly",
        "cap_micros": 1_000_000,
        "thresholds_bps": (8000, 9500),
        "on_exhaustion": "block",
    }
    values.update(overrides)
    return BudgetDef(**values)  # type: ignore[arg-type]


def test_periods_roll_over_at_year_end() -> None:
    assert period_key("daily", AT) == "2026-12-31"
    assert period_key("monthly", AT) == "2026-12"
    assert period_key("workflow", AT) == "lifetime"
    start, end = period_bounds("monthly", AT) or (None, None)
    assert (start, end) == (datetime(2026, 12, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC))
    assert period_bounds("request", AT) is None
    assert bucket_expiry(Bucket("tenant", "gct", "monthly", "2026-12"), AT) > end


def test_bucket_keys_round_trip() -> None:
    bucket = Bucket("workflow", "gct-incident-agent/wf-1", "workflow", "lifetime")
    assert Bucket.parse(bucket.key) == bucket
    assert Bucket("gpu_pool", "p", "daily", "2026-12-31").meter == "gpu"
    with pytest.raises(ValueError, match="malformed"):
        Bucket.parse("tenant|gct")


def test_bucket_keys_carry_their_money_domain() -> None:
    illustrative = Bucket("tenant", "gct", "daily", "2026-12-31")
    assert illustrative.domain == ILLUSTRATIVE and illustrative.key == "tenant|gct|daily|2026-12-31"
    for domain in (LIVE, visitor_domain("usr_0123456789abcdef")):
        bucket = Bucket("workflow", "gct-incident-agent/wf-1", "workflow", "lifetime", domain)
        assert bucket.key.startswith(f"{domain}:workflow|") and Bucket.parse(bucket.key) == bucket


def test_runs_that_cannot_spend_money_are_illustrative() -> None:
    visitor = ("public_visitor",)
    assert run_domain("live", roles=visitor, pseudonym="usr_a") == LIVE
    assert run_domain("record", roles=(), pseudonym=None) == LIVE
    assert run_domain("replay", roles=(), pseudonym="usr_a") == ILLUSTRATIVE
    assert run_domain("simulated", roles=visitor, pseudonym="usr_a") == visitor_domain("usr_a")


def test_live_buckets_commit_money_only() -> None:
    chain = (("tenant", "gct"),)
    lines = (ReservationLine("claude-flagship", 700),)
    charges = [
        Charge("claude-flagship", 90, provenance=Provenance.MEASURED),
        Charge("claude-flagship", 40, provenance=Provenance.SIMULATED),
    ]
    live = commit(allocate(chain, 700, lines, AT, domain=LIVE), 130, charges, PRICES.pools, AT)
    assert live["live:tenant|gct|monthly|2026-12"].committed_micros == 90
    assert live["live:model|claude-flagship|daily|2026-12-31"].committed_micros == 90
    shown = commit(allocate(chain, 700, lines, AT, domain=ILLUSTRATIVE), 130, charges, PRICES.pools, AT)
    assert shown["tenant|gct|monthly|2026-12"].committed_micros == 130


def test_public_demo_counts_api_and_gpu() -> None:
    chain = scope_chain(
        tenant_id="gct",
        department_id=None,
        application_id="app",
        principal_pseudonym="usr_v",
        workflow_id=None,
        public_demo=True,
    )
    assert chain[:2] == (("tenant", "gct"), ("public_demo", "gct"))
    lines = (
        ReservationLine("enterprise-cim", 0, gpu_pool="enterprise-cim", gpu_micros=50),
        ReservationLine("gct-qwen-finetuned", 0, gpu_pool="gct-qwen-finetuned", gpu_micros=120),
    )
    allocations = allocate(chain, 300, lines, AT, domain=LIVE)
    assert allocations["live:public_demo|gct|daily|2026-12-31"] == 300 + 50 + 120
    assert allocations["live:tenant|gct|daily|2026-12-31"] == 300
    charges = [Charge("gct-qwen-finetuned", 0, gpu_micros=1000, provenance=Provenance.MEASURED)]
    committed = commit(allocations, 0, charges, PRICES.pools, AT)
    demo = committed["live:public_demo|gct|daily|2026-12-31"]
    assert (demo.committed_micros, demo.provenance) == (1000, Provenance.ESTIMATED)
    assert committed["live:tenant|gct|daily|2026-12-31"].committed_micros == 0


def test_internet_gpu_counts_estimated_gpu_only() -> None:
    # ADR-0027: an internet key call's live runs draw on the tenant's internet GPU scope, which holds and
    # commits their estimated GPU cost across every pool and never their API spend.
    chain = scope_chain(
        tenant_id="gct",
        department_id=None,
        application_id="gct-api-sandbox",
        principal_pseudonym=None,
        workflow_id=None,
        internet_gpu=True,
    )
    assert chain[:2] == (("tenant", "gct"), (INTERNET_GPU, "gct"))
    assert Bucket(INTERNET_GPU, "gct", "monthly", "2026-12").meter == "gpu"
    lines = (
        ReservationLine("claude-flagship", 300),
        ReservationLine("enterprise-cim", 0, gpu_pool="enterprise-cim", gpu_micros=50),
        ReservationLine("gct-qwen-finetuned", 0, gpu_pool="gct-qwen-finetuned", gpu_micros=120),
    )
    allocations = allocate(chain, 300, lines, AT, domain=LIVE)
    assert allocations["live:internet_gpu|gct|monthly|2026-12"] == 50 + 120
    assert allocations["live:tenant|gct|monthly|2026-12"] == 300
    charges = [
        Charge("claude-flagship", 200, provenance=Provenance.MEASURED),
        Charge("gct-qwen-finetuned", 0, gpu_micros=1000, provenance=Provenance.MEASURED),
    ]
    committed = commit(allocations, 200, charges, PRICES.pools, AT)
    gpu = committed["live:internet_gpu|gct|monthly|2026-12"]
    assert (gpu.committed_micros, gpu.provenance) == (1000, Provenance.ESTIMATED)  # never measured
    assert committed["live:tenant|gct|monthly|2026-12"].committed_micros == 200
    without = scope_chain(
        tenant_id="gct", department_id=None, application_id="a", principal_pseudonym=None, workflow_id=None
    )
    assert INTERNET_GPU not in dict(without)


def test_visitor_buckets_expire_soon_after_their_period() -> None:
    visitor = Bucket("tenant", "gct", "daily", "2026-12-31", visitor_domain("usr_a"))
    assert bucket_expiry(visitor, AT) == datetime(2027, 1, 2, tzinfo=UTC)
    shared = Bucket("tenant", "gct", "daily", "2026-12-31")
    assert bucket_expiry(shared, AT) == datetime(2027, 1, 9, tzinfo=UTC)


def test_scope_chain_is_hierarchical() -> None:
    chain = scope_chain(
        tenant_id="gct",
        department_id="network-ops",
        application_id="gct-incident-agent",
        principal_pseudonym="usr_1",
        workflow_id="wf-1",
    )
    assert chain == (
        ("tenant", "gct"),
        ("department", "network-ops"),
        ("application", "gct-incident-agent"),
        ("user", "usr_1"),
        ("workflow", "gct-incident-agent/wf-1"),
        ("request", "gct-incident-agent"),
    )


def test_allocation_holds_envelope_on_shared_scopes_and_lines_on_models() -> None:
    chain = scope_chain(
        tenant_id="gct", department_id=None, application_id="app", principal_pseudonym=None, workflow_id="w"
    )
    lines = (
        ReservationLine("claude-flagship", 700),
        ReservationLine("gct-qwen-finetuned", 0, gpu_pool="gct-qwen-finetuned", gpu_micros=120),
    )
    allocations = allocate(chain, 700, lines, AT, domain=ILLUSTRATIVE)
    assert allocations["tenant|gct|monthly|2026-12"] == 700
    assert allocations["application|app|daily|2026-12-31"] == 700
    assert allocations["workflow|app/w|workflow|lifetime"] == 700
    assert allocations["model|claude-flagship|monthly|2026-12"] == 700
    assert allocations["model|gct-qwen-finetuned|daily|2026-12-31"] == 0
    assert allocations["gpu_pool|gct-qwen-finetuned|monthly|2026-12"] == 120
    assert not any(key.startswith("request|") for key in allocations), "request caps are stateless"


def test_commit_attributes_actuals_per_bucket() -> None:
    chain = (("tenant", "gct"), ("application", "app"))
    lines = (ReservationLine("claude-flagship", 700), ReservationLine("gemini-flash-lite", 300))
    allocations = allocate(chain, 700, lines, AT, domain=ILLUSTRATIVE)
    charges = [
        Charge("claude-flagship", 90, provenance=Provenance.MEASURED),
        Charge("gct-qwen-finetuned", 0, gpu_micros=7, provenance=Provenance.SIMULATED),
    ]
    result = commit(allocations, 90, charges, PRICES.pools, AT)
    assert result["tenant|gct|monthly|2026-12"].committed_micros == 90
    assert result["tenant|gct|monthly|2026-12"].provenance is Provenance.SIMULATED  # least trustworthy
    assert result["model|claude-flagship|daily|2026-12-31"].committed_micros == 90
    assert result["model|gemini-flash-lite|daily|2026-12-31"].committed_micros == 0
    assert result["model|gemini-flash-lite|daily|2026-12-31"].reserved_micros == 300
    # A model that was never reserved for is still committed: it is real spend.
    pool = result["gpu_pool|gct-qwen-finetuned|monthly|2026-12"]
    assert (pool.reserved_micros, pool.committed_micros, pool.provenance) == (0, 7, Provenance.SIMULATED)


def test_least_trusted_orders_provenance() -> None:
    assert least_trusted([Provenance.MEASURED, Provenance.ESTIMATED]) is Provenance.ESTIMATED
    assert least_trusted([Provenance.REPLAYED, Provenance.SIMULATED]) is Provenance.SIMULATED
    assert least_trusted([]) is None


def test_envelope_covers_retries_cim_and_baseline() -> None:
    models = {m.id: m for m in CATALOG.models.models}
    upstream = [models["gemini-flash-lite"], models["claude-flagship"], models["gct-qwen-finetuned"]]
    sizing = {"upstream_attempts": 2, "input_tokens": 1000, "max_output_tokens": 500}
    envelope = build_envelope(
        PRICES, upstream=upstream, cim=models["enterprise-cim"], baseline=models["claude-flagship"], **sizing
    )
    claude = 2 * PRICES.cost_micros("claude-flagship", input_tokens=1000, output_tokens=500)
    baseline = claude // 2
    assert envelope.amount_micros == claude + baseline  # most expensive upstream + CIM ($0) + baseline
    lines = {line.model_id: line for line in envelope.lines}
    assert lines["claude-flagship"].amount_micros == claude + baseline
    assert lines["gct-qwen-finetuned"].amount_micros == 0
    assert lines["gct-qwen-finetuned"].gpu_pool == "gct-qwen-finetuned"
    assert lines["gct-qwen-finetuned"].gpu_micros == PRICES.gpu_worst_case_micros(
        "gct-qwen-finetuned", attempts=2
    )
    assert lines["enterprise-cim"].gpu_micros == PRICES.gpu_worst_case_micros("enterprise-cim", attempts=1)
    assert lines["claude-flagship"].gpu_pool is None


def test_the_baseline_line_is_sized_with_its_own_output_budget() -> None:
    models = {m.id: m for m in CATALOG.models.models}
    sizing = {"upstream_attempts": 2, "input_tokens": 1000, "max_output_tokens": 500}
    gemini, claude = models["gemini-flash-lite"], models["claude-flagship"]
    upstream = 2 * PRICES.cost_micros("gemini-flash-lite", input_tokens=1000, output_tokens=500)
    # Omitted: the baseline is sized like the upstream call, as before.
    default = build_envelope(PRICES, upstream=[gemini], baseline=claude, **sizing)
    assert default.amount_micros == upstream + PRICES.cost_micros(
        "claude-flagship", input_tokens=1000, output_tokens=500
    )
    larger = build_envelope(
        PRICES, upstream=[gemini], baseline=claude, baseline_max_output_tokens=6144, **sizing
    )
    baseline = PRICES.cost_micros("claude-flagship", input_tokens=1000, output_tokens=6144)
    assert larger.amount_micros == upstream + baseline
    assert {line.model_id: line.amount_micros for line in larger.lines} == {
        "gemini-flash-lite": upstream,
        "claude-flagship": baseline,
    }
    assert larger.upstream_micros == default.upstream_micros  # upstream attempts keep the request's budget


def test_gpu_estimates_are_integer_and_rounded() -> None:
    # $2.50/h for 1.44 s = 1,000 micro-USD; 1 s = 694.4 rounds to 694. The worst case is the full timeout
    # per attempt (2 x 120 s = 166,666.7), rounded up.
    assert PRICES.gpu_estimate_micros("gct-qwen-finetuned", 1440.0) == 1000
    assert PRICES.gpu_estimate_micros("gct-qwen-finetuned", 1000.4) == 694
    assert PRICES.gpu_estimate_micros("claude-flagship", 1000.0) == 0
    assert PRICES.gpu_worst_case_micros("gct-qwen-finetuned", attempts=2) == 166_667


def test_lower_cost_narrowing_stays_inside_the_pool() -> None:
    models = {m.id: m for m in CATALOG.models.models}
    pool = [models["claude-flagship"], models["gemini-flash-lite"], models["gct-qwen-finetuned"]]
    costs = upstream_costs(PRICES, pool, upstream_attempts=2, input_tokens=1000, max_output_tokens=500)
    shared = QuotaDeniedError("x", rule_id="b", on_exhaustion="lower_cost_route", scope_type="application")
    narrowed = narrow_for_lower_cost(pool, pool, shared, costs, PRICES)
    assert [m.id for m in narrowed] == ["gemini-flash-lite", "gct-qwen-finetuned"]
    model = QuotaDeniedError("x", rule_id="b", scope_type="model", scope_id="gemini-flash-lite")
    assert [m.id for m in narrow_for_lower_cost(narrowed, narrowed, model, costs, PRICES)] == [
        "gct-qwen-finetuned"
    ]
    gpu = QuotaDeniedError("x", rule_id="b", scope_type="gpu_pool", scope_id="gct-qwen-finetuned")
    only_local = [models["gct-qwen-finetuned"]]
    assert narrow_for_lower_cost(only_local, only_local, gpu, costs, PRICES) == []
    assert narrow_for_lower_cost(only_local, only_local, shared, costs, PRICES) == []


def test_thresholds_and_crossings() -> None:
    budget = _budget()
    assert budget.crossings(700_000, 810_000) == [8000]
    assert budget.crossings(810_000, 1_000_000) == [9500, 10_000]
    assert budget.crossings(960_000, 990_000) == []
    assert budget.crossed(950_000) == [8000, 9500]
    assert to_bps(0.8) == 8000 and to_bps("0.955") == 9550


def test_book_prefers_specific_budgets_over_templates() -> None:
    template = _budget(id="t", scope_type="user", scope_id="default", cap_micros=10)
    specific = _budget(id="s", scope_type="user", scope_id="usr_1", cap_micros=99)
    book = make_book("gct", [template, specific])
    assert book.for_bucket(Bucket("user", "usr_1", "monthly", "2026-12")) == specific
    assert book.for_bucket(Bucket("user", "usr_2", "monthly", "2026-12")) == template
    assert book.for_bucket(Bucket("user", "usr_2", "daily", "2026-12-31")) is None
    assert make_book("gct", [specific, template]).version == book.version
    assert make_book("gct", [template]).version != book.version


def test_forecast_is_a_linear_run_rate() -> None:
    mid_month = datetime(2026, 10, 16, 12, tzinfo=UTC)  # half of a 31-day month has elapsed
    assert forecast_micros("monthly", 1_000_000, mid_month) == 2_000_000
    assert forecast_micros("workflow", 1_000_000, mid_month) is None


def test_budget_ids_stay_within_column_width() -> None:
    assert (
        budget_id("application", "gct-support-chatbot", "monthly")
        == "bud-application-gct-support-chatbot-monthly"
    )
    long_id = budget_id("workflow", "gct-incident-agent/" + "w" * 40, "workflow")
    assert len(long_id) <= 64 and "/" not in long_id
