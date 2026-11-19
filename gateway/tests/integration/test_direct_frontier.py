"""Direct-frontier estimate in the chat response (live demo counter): the frontier's list price of the request
with no AiVa optimization. It is computed, never called, so the sovereign path stays sovereign, and it is
reported apart from the measured baseline comparison."""

from typing import Any

from sqlalchemy import select

from aiva_gateway.db.models import ModelAttemptRow
from aiva_gateway.finops.frontier import direct_frontier_estimate
from aiva_gateway.reporting.lines import metered_original
from tests.conftest import chat_body, with_overrides
from tests.integration.test_budgets import SHARED_CACHE_APP, DictCache
from tests.integration.test_gpu_cost import CONFIDENTIAL, run, timed

EXTERNAL = {"gemini-flash-lite", "deepseek-v4-1-flash", "claude-flagship"}
QUESTION = chat_body("What are the Europe roaming rates?", max_output_tokens=64)


def frontier(services: Any, input_tokens: int, output_tokens: int) -> int:
    return int(
        services.prices.cost_micros("claude-flagship", input_tokens=input_tokens, output_tokens=output_tokens)
    )


async def test_the_sovereign_path_is_estimated_without_any_frontier_call(app_client, services, db) -> None:
    body = await run(app_client, timed(services), CONFIDENTIAL)  # a live gateway: measured usage
    assert body["status"] == "completed" and body["execution_path"] == "sovereign-direct"
    attempts = list(await db.scalars(select(ModelAttemptRow).where(ModelAttemptRow.run_id == body["run_id"])))
    assert attempts and not {a.model_id for a in attempts} & EXTERNAL, "nothing was sent to the frontier"
    cost, usage = body["cost"], body["usage"]
    assert cost["direct_frontier_provenance"] == "estimated" and cost["provenance"] == "measured"
    assert cost["direct_frontier_estimated_micros"] > 0 == cost["total_api_micros"]
    assert cost["rate_card_version"] == services.prices.version
    # The original context on the served attempt's metered basis, at the frontier's list price.
    original = metered_original(
        usage["upstream_submitted_input_tokens"],
        usage["original_context_tokens"],
        usage["constructed_context_tokens"] or usage["original_context_tokens"],
    )
    expected = frontier(services, original, usage["upstream_output_tokens"])
    assert cost["direct_frontier_estimated_micros"] == expected


async def test_it_is_reported_apart_from_the_measured_baseline(app_client, services) -> None:
    body = await run(app_client, timed(services), {**QUESTION, "baseline_comparison": True})
    assert body["status"] == "completed", body.get("error")
    cost = body["cost"]
    assert cost["baseline_micros"] > 0 and cost["direct_frontier_estimated_micros"] > 0
    assert cost["direct_frontier_provenance"] == "estimated"
    # The baseline call is measured spend inside total_api_micros; the estimate never is.
    assert cost["total_api_micros"] == cost["upstream_micros"] + cost["cim_micros"] + cost["baseline_micros"]


async def test_a_blocked_run_has_no_estimate(app_client, services) -> None:
    body = await run(app_client, services, chat_body("Use key sk-ant-abcdefghijklmnopqrstuvwxyz123"))
    assert body["status"] == "blocked"
    assert body["cost"]["direct_frontier_estimated_micros"] is None
    assert body["cost"]["direct_frontier_provenance"] is None


async def test_a_cache_hit_shows_the_frontier_call_it_avoided(app_client, services) -> None:
    cache = DictCache()
    svc = with_overrides(services, cache=cache)
    async with app_client(SHARED_CACHE_APP, svc=svc) as client:
        first = (await client.post("/v1/ai/chat", json=QUESTION)).json()
        repeat = (await client.post("/v1/ai/chat", json=QUESTION)).json()
    assert first["status"] == "completed" and repeat["execution_path"] == "cache-return"
    [entry] = cache.entries.values()
    expected, label = direct_frontier_estimate(
        services.prices, served=None, context_before=0, context_after=0, cache_hit=entry
    )
    assert repeat["cost"]["direct_frontier_estimated_micros"] == expected
    assert expected == frontier(services, entry.modeled_input_tokens, entry.modeled_output_tokens) > 0
    assert repeat["cost"]["direct_frontier_provenance"] == label.value == "simulated"  # simulated gateway
    assert repeat["cost"]["total_api_micros"] == 0
