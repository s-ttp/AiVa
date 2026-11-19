"""Billed tool calls (finops.tools): the web-search rate card, the reservation that covers a search before it
runs, and the `tool_call` charge, which counts in the shared scopes only. Integer micro-USD at rc-2026-10-21
(Tavily, unchanged since rc-2026-10-20: $0.008 per API credit; a basic search is 1 credit, an advanced
one 2)."""

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aiva_gateway.catalog import load_catalog
from aiva_gateway.config import REPO_ROOT
from aiva_gateway.core.enums import ChargeType, Provenance
from aiva_gateway.core.errors import AivaError
from aiva_gateway.finops.envelope import build_envelope
from aiva_gateway.finops.ledger import settlement_charges, usage_rows
from aiva_gateway.finops.pricing import PriceBook
from aiva_gateway.finops.scopes import LIVE, Bucket, commit
from aiva_gateway.finops.tools import check_tool_call, record_tool_call
from aiva_gateway.pipeline.context import ToolCharge
from aiva_gateway.pipeline.interfaces import Charge, Reservation
from tests.unit.test_cache_keys import run

CATALOG = load_catalog(REPO_ROOT / "config")
PRICES = PriceBook(CATALOG)
SEARCH = "web.search"
NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def test_a_basic_search_costs_one_credit_at_the_pinned_rate_card() -> None:
    assert CATALOG.rate_cards.version == "rc-2026-10-21"
    assert PRICES.tool_rate_card_id(SEARCH) == "rc-tavily-search"
    assert PRICES.tool_cost_micros(SEARCH, 1) == 8_000  # $0.008, the free monthly credits not netted
    assert PRICES.tool_cost_micros(SEARCH, 2) == 16_000  # an advanced search
    assert PRICES.tool_cost_micros("kb.search", 3) == 0 and PRICES.tool_rate_card_id("kb.search") is None
    with pytest.raises(ValueError):
        PRICES.tool_cost_micros(SEARCH, -1)


@pytest.mark.parametrize(
    "tool_id, per_unit, message",
    [
        ("web.lookup", "0.01", "unknown tool web.lookup"),
        ("web.search", "0.01", "web.search is priced more than once"),
        ("kb.search", "0.0000001", "whole micro-USD"),
    ],
    ids=["unknown-tool", "duplicate", "sub-micro-price"],
)
def test_the_loader_rejects_a_bad_tool_rate_card(
    tmp_path: Path, tool_id: str, per_unit: str, message: str
) -> None:
    card = (
        f"{{id: rc-x, tool_id: {tool_id}, unit: credit, per_unit: '{per_unit}', "
        "effective_from: 2026-10-20, source: s}"
    )
    config = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", config)
    path = config / "rate_cards.yaml"
    path.write_text(path.read_text(encoding="utf-8") + f"  - {card}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_catalog(config)


def test_the_envelope_reserves_the_planned_searches_outside_any_model_line() -> None:
    gemini = CATALOG.model("gemini-flash-lite")
    assert gemini is not None
    plain = build_envelope(
        PRICES, upstream=[gemini], upstream_attempts=2, input_tokens=500, max_output_tokens=100
    )
    searched = build_envelope(
        PRICES,
        upstream=[gemini],
        upstream_attempts=2,
        input_tokens=500,
        max_output_tokens=100,
        tools={SEARCH: 3},
    )
    assert searched.tool_micros == 3 * 8_000 and plain.tool_micros == 0
    assert searched.amount_micros == plain.amount_micros + 24_000
    assert searched.lines == plain.lines  # a tool is never a model line


def reserved_run(tool_micros: int, *, reserved: bool = True):  # type: ignore[no-untyped-def]
    ctx = run("Research the synthetic GCT roaming market.")
    if reserved:
        ctx.reservation = Reservation("res-1", 100_000, (), Provenance.MEASURED)
        ctx.reserved_tool_micros = tool_micros
    return ctx


def test_a_search_runs_only_within_what_the_reservation_holds() -> None:
    ctx = reserved_run(16_000)
    assert check_tool_call(ctx, PRICES, SEARCH, 1) == 8_000
    record_tool_call(ctx, PRICES, SEARCH, 1, Provenance.MEASURED)
    assert check_tool_call(ctx, PRICES, SEARCH, 1) == 8_000  # the second basic search still fits
    record_tool_call(ctx, PRICES, SEARCH, 1, Provenance.MEASURED)
    with pytest.raises(AivaError) as refused:
        check_tool_call(ctx, PRICES, SEARCH, 1)  # a third would pass what was reserved: never made
    assert refused.value.code == "RESERVATION_INSUFFICIENT"
    assert ctx.tool_cost_micros == 16_000 and ctx.api_cost_micros == 16_000  # no model attempt yet


def test_no_search_runs_before_a_reservation_exists() -> None:
    with pytest.raises(AivaError) as refused:
        check_tool_call(reserved_run(0, reserved=False), PRICES, SEARCH, 1)
    assert refused.value.code == "RESERVATION_INSUFFICIENT"


def test_a_call_billed_past_the_plan_is_still_recorded() -> None:
    ctx = reserved_run(8_000)
    charge = record_tool_call(ctx, PRICES, SEARCH, 2, Provenance.MEASURED)  # the provider billed 2 credits
    assert charge.cost_micros == 16_000 and ctx.tool_charges == [charge]  # real spend is never dropped


def test_tool_calls_settle_per_tool_with_their_least_trusted_label() -> None:
    calls = [
        ToolCharge("tc-1", SEARCH, 1, 8_000, Provenance.MEASURED, NOW),
        ToolCharge("tc-2", SEARCH, 2, 16_000, Provenance.ESTIMATED, NOW),
    ]
    [charge] = settlement_charges([], calls)
    assert charge == Charge(SEARCH, 24_000, provenance=Provenance.ESTIMATED, kind="tool")


def test_a_tool_charge_counts_in_the_shared_scopes_only() -> None:
    tenant = Bucket("tenant", "gct", "daily", "2026-10-20", LIVE)
    model = Bucket("model", "gemini-flash-lite", "daily", "2026-10-20", LIVE)
    allocations = {tenant.key: 50_000, model.key: 30_000}
    charges = [
        Charge("gemini-flash-lite", 1_000, provenance=Provenance.MEASURED),
        Charge(SEARCH, 8_000, provenance=Provenance.MEASURED, kind="tool"),
    ]
    result = commit(allocations, 9_000, charges, PRICES.pools, NOW)
    assert result[tenant.key].committed_micros == 9_000  # the search counts in the tenant budget
    assert result[model.key].committed_micros == 1_000  # but never in a model's
    assert not any(Bucket.parse(key).scope_id == SEARCH for key in result)  # nor gets a bucket of its own


def test_a_simulated_search_never_counts_in_a_live_budget() -> None:
    tenant = Bucket("tenant", "gct", "daily", "2026-10-20", LIVE)
    charges = [
        Charge("gemini-flash-lite", 1_000, provenance=Provenance.MEASURED),
        Charge(SEARCH, 8_000, provenance=Provenance.SIMULATED, kind="tool"),
    ]
    result = commit({tenant.key: 50_000}, 9_000, charges, PRICES.pools, NOW)
    assert result[tenant.key].committed_micros == 1_000


def test_each_search_is_one_tool_call_ledger_row() -> None:
    ctx = reserved_run(16_000)
    record_tool_call(ctx, PRICES, SEARCH, 1, Provenance.MEASURED)
    [row] = usage_rows(ctx, CATALOG)
    assert (row.charge_type, row.attempt_id, row.rate_card_id) == (
        ChargeType.TOOL_CALL.value,
        None,
        "rc-tavily-search",
    )
    assert (row.amount_micros, row.measured_or_estimated) == (8_000, "measured")
    assert row.rate_card_version == CATALOG.rate_cards.version and row.reservation_id == "res-1"
