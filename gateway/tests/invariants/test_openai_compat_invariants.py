"""LLD Appendix B invariants through the OpenAI-compatible API (ADR-0026): the facade runs the native
pipeline, so every rule holds when the request arrives as an OpenAI chat completion."""

from sqlalchemy import select, text

from aiva_gateway.db.models import ModelAttemptRow, PolicyEventRow, UsageLedgerRow
from aiva_gateway.inference.registry import AdapterRegistry
from aiva_gateway.inference.simulated import SimulatedInferenceAdapter
from tests.conftest import PEERS, with_overrides

PATH = "/v1/chat/completions"
EXTERNAL = {"gemini-flash-lite", "deepseek-v4-1-flash", "claude-flagship"}
FRAUD = "gct-fraud-investigator"  # its classification floor is confidential


async def _attempts(db, run_id: str) -> list[ModelAttemptRow]:  # type: ignore[no-untyped-def]
    rows = await db.execute(select(ModelAttemptRow).where(ModelAttemptRow.run_id == run_id))
    return sorted(rows.scalars().all(), key=lambda a: a.sequence)


def _ask(content: str, **extra: object) -> dict[str, object]:
    return {"messages": [{"role": "user", "content": content}], **extra}


async def test_confidential_takes_the_sovereign_path_through_the_facade(app_client, db) -> None:  # type: ignore[no-untyped-def]
    async with app_client(FRAUD) as client:
        body = (await client.post(PATH, json=_ask("Summarise synthetic alert FR-2026-0412"))).json()
    aiva = body["aiva"]
    assert aiva["effective_classification"] == "confidential"
    assert aiva["execution_path"] == "sovereign-direct" and body["model"] == "gct-qwen-finetuned"
    assert aiva["cim"] == "CIM bypassed by mandatory sovereignty policy"
    attempts = await _attempts(db, body["id"])
    assert [a.stage for a in attempts] == ["upstream"] and not {a.model_id for a in attempts} & EXTERNAL


async def test_a_requested_external_model_never_reaches_confidential_data(app_client, db) -> None:  # type: ignore[no-untyped-def]
    for model in sorted(EXTERNAL):
        async with app_client(FRAUD) as client:
            response = await client.post(PATH, json=_ask("Summarise synthetic alert FR-9", model=model))
        body = response.json()
        assert response.status_code == 403 and body["error"]["code"] == "model_not_allowed", model
        assert await _attempts(db, body["aiva"]["run_id"]) == []


async def test_the_sovereign_path_never_falls_back_through_the_facade(  # type: ignore[no-untyped-def]
    services, make_client, credentials, db
) -> None:
    down = SimulatedInferenceAdapter(unavailable_models={"gct-qwen-finetuned"})
    broken = with_overrides(services, adapters=AdapterRegistry(services.catalog, adapters={}, default=down))
    async with make_client(credentials[FRAUD], peer=PEERS[FRAUD], svc=broken) as client:
        response = await client.post(PATH, json=_ask("Summarise FR-9"))
    body = response.json()
    assert response.status_code == 503
    assert body["error"]["code"] == "sovereign_endpoint_unavailable" and body["error"]["type"] == "api_error"
    attempts = await _attempts(db, body["aiva"]["run_id"])
    assert [a.model_id for a in attempts] == ["gct-qwen-finetuned", "gct-qwen-finetuned"]  # one bounded retry


async def test_a_policy_block_is_a_block_with_no_usage(app_client, db) -> None:  # type: ignore[no-untyped-def]
    async with app_client("gct-support-chatbot") as client:
        response = await client.post(PATH, json=_ask("Please use password = hunter2pass"))
    body = response.json()
    assert response.status_code == 403
    assert body["error"]["message"] == "Blocked by AI policy." and body["error"]["code"] == "policy_blocked"
    assert "GCT-SEC-002" in body["aiva"]["rule_ids"] and body["aiva"]["status"] == "blocked"
    run_id = body["aiva"]["run_id"]
    ledger = (await db.execute(select(UsageLedgerRow).where(UsageLedgerRow.run_id == run_id))).all()
    assert ledger == [] and await _attempts(db, run_id) == []
    events = (await db.execute(select(PolicyEventRow).where(PolicyEventRow.run_id == run_id))).scalars().all()
    assert [e.stage for e in events] == ["request"]


async def test_policy_is_evaluated_before_a_required_model_is_considered(app_client, db) -> None:  # type: ignore[no-untyped-def]
    async with app_client(FRAUD) as client:
        body = (await client.post(PATH, json=_ask("Summarise FR-9", model="claude-flagship"))).json()
    run_id = body["aiva"]["run_id"]
    events = (await db.execute(select(PolicyEventRow).where(PolicyEventRow.run_id == run_id))).scalars().all()
    assert [e.stage for e in events] == ["request"]  # evaluated; no egress was ever reached


async def test_no_prompt_text_in_records_or_spans_through_the_facade(app_client, db) -> None:  # type: ignore[no-untyped-def]
    canary = "CANARY-0a1f7"
    payload = _ask(f"{canary} roaming question", metadata={"note": canary}, user=canary)
    async with app_client("gct-support-chatbot") as client:
        body = (await client.post(PATH, json=payload)).json()
    assert body["object"] == "chat.completion"
    tables = (
        (
            await db.execute(
                text(
                    "select table_name from information_schema.tables "
                    "where table_schema='public' and table_type='BASE TABLE'"
                )
            )
        )
        .scalars()
        .all()
    )
    for table in tables:
        hits = await db.scalar(text(f"select count(*) from {table} t where t::text like '%{canary}%'"))
        assert hits == 0, f"prompt text leaked into {table}"
