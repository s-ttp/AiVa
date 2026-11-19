"""Quality tier on the coding assistant (owner decision for scenario 2): at `critical`, RLCD serves an
internal coding request with the frontier model, inside the application's policy, and the per-request cap
admits the output budget its thinking needs. A baseline comparison against the model that served the run
measures nothing, so it is skipped: no call, no charge, nothing reserved for it."""

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from aiva_gateway.db.models import ModelAttemptRow, TraceSpanRow, UsageLedgerRow
from tests.conftest import chat_body, console

CODING = "Write a Python function that parses synthetic CDR records and flags roaming sessions over 2 GB."


def coding(**extra: Any) -> dict[str, Any]:
    return chat_body(
        CODING,
        scenario_id="coding-assistant",
        data_classification="internal",
        **extra,
        **console("gct-coding-assistant"),
    )


async def _attempts(db, run_id: str) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    rows = (await db.execute(select(ModelAttemptRow).where(ModelAttemptRow.run_id == run_id))).scalars()
    return [(a.stage, a.model_id) for a in sorted(rows, key=lambda a: a.sequence)]


async def test_a_critical_coding_request_is_served_by_the_frontier_model(tester_client, db) -> None:  # type: ignore[no-untyped-def]
    async with tester_client() as client:
        critical = (await client.post("/v1/ai/chat", json=coding(quality_tier="critical"))).json()
        standard = (await client.post("/v1/ai/chat", json=coding())).json()
    assert critical["status"] == "completed", critical.get("error")
    assert critical["selected_model"] == "claude-flagship"
    assert "quality tier critical requires rank >= 3" in critical["routing"]["reasons"]
    policy = critical["policy"]
    assert policy["action"] == "allow" and policy["effective_classification"] == "internal"
    assert "claude-flagship" in policy["allowed_models"]
    assert await _attempts(db, critical["run_id"]) == [("upstream", "claude-flagship")]
    # At the default tier the same request goes to the cheapest qualified model, never the frontier one.
    assert standard["status"] == "completed" and standard["selected_model"] != "claude-flagship"


async def test_no_baseline_comparison_when_the_frontier_model_served_the_run(tester_client, db) -> None:  # type: ignore[no-untyped-def]
    async with tester_client() as client:
        plain = (await client.post("/v1/ai/chat", json=coding(quality_tier="critical"))).json()
        compared = (
            await client.post("/v1/ai/chat", json=coding(quality_tier="critical", baseline_comparison=True))
        ).json()
    assert compared["status"] == "completed", compared.get("error")
    assert compared["selected_model"] == "claude-flagship"
    assert await _attempts(db, compared["run_id"]) == [("upstream", "claude-flagship")]  # no second call
    assert compared["cost"]["baseline_micros"] == 0
    charges = (
        await db.execute(
            select(UsageLedgerRow.charge_type).where(UsageLedgerRow.run_id == compared["run_id"])
        )
    ).scalars()
    assert "baseline_comparison" not in set(charges)
    # Nothing was held for a comparison that cannot run: the same envelope as without one.
    assert compared["budget"]["reserved_micros"] == plain["budget"]["reserved_micros"] > 0
    status = await db.scalar(
        select(TraceSpanRow.attributes["aiva.baseline.status"].astext).where(
            TraceSpanRow.run_id == compared["run_id"],
            TraceSpanRow.attributes.has_key("aiva.baseline.status"),
        )
    )
    assert status == "same-as-served"


@pytest.mark.parametrize("max_output_tokens", [4096, 6144])
async def test_the_request_cap_admits_a_frontier_coding_answer_with_room_to_think(  # type: ignore[no-untyped-def]
    tester_client, services, max_output_tokens
) -> None:
    # Opus thinking counts against max_output_tokens, so S2 asks for more than the 1,024 default. The
    # envelope holds both upstream attempts at the full budget ($20/MTok output, 20 micro-USD per token),
    # far inside the tenant's $2 per-request cap, so the cap's lower_cost_route never steps in.
    [cap] = [b for b in services.catalog.tenant.budgets if b.scope_type == "request"]
    assert (cap.cap_usd, cap.on_exhaustion) == (Decimal("2.00"), "lower_cost_route")
    payload = coding(quality_tier="critical", max_output_tokens=max_output_tokens, baseline_comparison=True)
    async with tester_client() as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed", body.get("error")
    assert body["selected_model"] == "claude-flagship"
    reserved = body["budget"]["reserved_micros"]
    assert 2 * max_output_tokens * 20 <= reserved < 300_000  # the cap is 2,000,000
