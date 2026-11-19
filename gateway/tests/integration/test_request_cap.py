"""Default per-request budget (config/tenant.yaml): a $2 worst-case envelope, `lower_cost_route` when over.

Envelopes are sized with the conservative input-token bound (about one token per UTF-8 byte), so a large
context on a Claude-eligible request exceeds $2 while cheaper eligible models fit. The re-plan only ever
narrows the policy-eligible set: confidential traffic stays on the sovereign path.
"""

from sqlalchemy import select

from aiva_gateway.db.models import ModelAttemptRow
from tests.conftest import chat_body, console

REQUEST_CAP = "bud-request-default-request"
EXTERNAL = {"gemini-flash-lite", "deepseek-v4-1-flash", "claude-flagship"}
LINE = "Roaming bundles for Europe include data and voice allowances per zone. "


def text(size: int) -> str:
    return (LINE * (size // len(LINE) + 1))[:size]


async def _models(db, run_id: str) -> list[str]:
    rows = await db.scalars(select(ModelAttemptRow.model_id).where(ModelAttemptRow.run_id == run_id))
    return list(rows)


async def test_over_cap_request_is_replanned_to_a_cheaper_eligible_model(tester_client, db) -> None:
    # Claude's worst case here is ~$2.27 (2 attempts x (120 KB x $4/M + 32,768 x $20/M)): over the cap.
    payload = chat_body(
        text(120_000),
        max_output_tokens=32_768,
        routing={"mode": "hybrid", "preferred_model": "gemini-flash-lite"},
        **console("gct-support-chatbot"),
    )
    async with tester_client() as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed", body.get("error")
    assert body["execution_path"] == "cim-assisted"
    assert body["selected_model"] == "gemini-flash-lite"
    assert any(
        f"lower-cost route: budget {REQUEST_CAP} excluded claude-flagship" in r
        for r in body["routing"]["reasons"]
    )
    assert body["budget"]["reserved_micros"] <= 2_000_000
    assert "claude-flagship" not in await _models(db, body["run_id"])


async def test_request_still_blocks_when_no_eligible_route_fits(tester_client, db) -> None:
    # The opt-in Claude baseline alone (~$2.10 for 360 KB at 32,768 output tokens) exceeds the cap and is not
    # an upstream candidate, so narrowing cannot make the envelope fit.
    evidence = [{"id": f"doc-{i}", "text": text(80_000)} for i in range(2)]
    payload = chat_body(
        text(200_000),
        evidence=evidence,
        max_output_tokens=32_768,
        baseline_comparison=True,
        **console("gct-support-chatbot"),
    )
    async with tester_client() as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "blocked" and body["outcome"] == "quota_denied"
    assert body["error"]["code"] == "QUOTA_EXCEEDED" and body["error"]["rule_ids"] == [REQUEST_CAP]
    assert "No eligible lower-cost route" in body["error"]["message"]
    assert await _models(db, body["run_id"]) == [], "nothing billable ran"


async def test_confidential_request_stays_sovereign_and_never_reroutes(tester_client, db) -> None:
    payload = chat_body(
        text(48_000),  # within GCT-Qwen's 16,384-token window (Endpoint B's vLLM max_model_len)
        data_classification="confidential",
        baseline_comparison=True,
        routing={"mode": "hybrid", "preferred_model": "claude-flagship"},
        **console("gct-fraud-investigator"),
    )
    async with tester_client() as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed", body.get("error")
    assert body["execution_path"] == "sovereign-direct"
    assert body["selected_model"] == "gct-qwen-finetuned"
    assert not any("lower-cost route" in r for r in body["routing"]["reasons"])
    models = await _models(db, body["run_id"])
    assert models and not set(models) & EXTERNAL
