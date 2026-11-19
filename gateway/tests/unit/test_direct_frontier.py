"""Direct-frontier estimate (finops.frontier): the pinned frontier's list price of the request with no AiVa
optimization, in integer micro-USD at rc-2026-10-15 (claude-flagship $4 input / $20 output per Mtok)."""

from datetime import UTC, datetime

import pytest

from aiva_gateway.catalog import load_catalog
from aiva_gateway.config import REPO_ROOT
from aiva_gateway.core.enums import AttemptStage, ChargeType, Provenance
from aiva_gateway.finops.frontier import direct_frontier_estimate
from aiva_gateway.finops.pricing import PriceBook
from aiva_gateway.pipeline.context import AttemptRecord
from aiva_gateway.pipeline.interfaces import CachedResponse

CATALOG = load_catalog(REPO_ROOT / "config")
PRICES = PriceBook(CATALOG)


def served(
    model_id: str, *, metered: int, output: int, label: Provenance = Provenance.MEASURED
) -> AttemptRecord:
    model = CATALOG.model(model_id)
    assert model is not None
    now = datetime.now(UTC)
    return AttemptRecord(
        attempt_id="att-1",
        stage=AttemptStage.UPSTREAM,
        model=model,
        started_at=now,
        completed_at=now,
        status="ok",
        input_tokens=metered,
        output_tokens=output,
        cached_input_tokens=0,
        reasoning_tokens=0,
        latency_ms=None,
        ttft_ms=None,
        provider_model_version="test",
        provenance=label,
        error_code=None,
        retry_of=None,
        charge_type=ChargeType.UPSTREAM_INFERENCE,
        cost_micros=0,
    )


def test_the_frontier_is_the_pinned_baseline_at_list_price() -> None:
    assert PRICES.frontier_model_id == CATALOG.models.frontier_baseline == "claude-flagship"
    price = PRICES.price("claude-flagship")
    assert (price.input_micros_per_mtok, price.output_micros_per_mtok) == (4_000_000, 20_000_000)


def test_the_full_original_context_is_priced_on_the_served_attempts_metered_basis() -> None:
    # Estimated 6,000 -> 1,000 tokens by the CIM, 2,000 metered: the original is 12,000 metered tokens.
    attempt = served("gemini-flash-lite", metered=2000, output=300)
    amount, label = direct_frontier_estimate(PRICES, served=attempt, context_before=6000, context_after=1000)
    assert amount == 12_000 * 4 + 300 * 20 == 54_000
    assert label is Provenance.ESTIMATED  # measured tokens, priced without a call: never measured


def test_a_frontier_served_run_without_pruning_costs_its_own_list_price() -> None:
    attempt = served("claude-flagship", metered=2000, output=300)
    amount, _ = direct_frontier_estimate(PRICES, served=attempt, context_before=1000, context_after=1000)
    assert amount == PRICES.cost_micros("claude-flagship", input_tokens=2000, output_tokens=300) == 14_000


def test_the_sovereign_path_is_still_estimated_and_never_sent() -> None:
    # GCT-Qwen has a $0 token price; the estimate prices what the frontier would have charged instead.
    attempt = served("gct-qwen-finetuned", metered=758, output=162)
    amount, label = direct_frontier_estimate(PRICES, served=attempt, context_before=764, context_after=764)
    assert attempt.cost_micros == 0
    assert amount == 758 * 4 + 162 * 20 == 6_272 and label is Provenance.ESTIMATED


def test_no_recorded_context_prices_the_metered_input() -> None:
    attempt = served("gemini-flash-lite", metered=900, output=100)
    amount, _ = direct_frontier_estimate(PRICES, served=attempt, context_before=0, context_after=0)
    assert amount == 900 * 4 + 100 * 20 == 5_600


def test_a_cache_hit_shows_the_avoided_frontier_call() -> None:
    hit = CachedResponse(
        source_run_id="run-src",
        model_id="gemini-flash-lite",
        content="cached",
        modeled_input_tokens=1500,
        modeled_output_tokens=250,
        modeled_cost_micros=1075,
        provenance=Provenance.MEASURED,
    )
    amount, label = direct_frontier_estimate(
        PRICES, served=None, context_before=0, context_after=0, cache_hit=hit
    )
    assert amount == 1500 * 4 + 250 * 20 == 11_000 and label is Provenance.ESTIMATED


@pytest.mark.parametrize("label", [Provenance.REPLAYED, Provenance.SIMULATED])
def test_replayed_or_simulated_tokens_keep_their_label(label: Provenance) -> None:
    attempt = served("gemini-flash-lite", metered=900, output=100, label=label)
    _, provenance = direct_frontier_estimate(PRICES, served=attempt, context_before=900, context_after=900)
    assert provenance is label


def test_no_answer_has_no_estimate() -> None:
    assert direct_frontier_estimate(PRICES, served=None, context_before=500, context_after=0) == (None, None)


def test_amounts_are_integer_micro_usd() -> None:
    # Sub-micro amounts round as the rate card does; the result is always an int.
    attempt = served("gemini-flash-lite", metered=1, output=1)
    amount, _ = direct_frontier_estimate(PRICES, served=attempt, context_before=1, context_after=1)
    assert isinstance(amount, int) and amount == PRICES.cost_micros(
        "claude-flagship", input_tokens=1, output_tokens=1
    )
