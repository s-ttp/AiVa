"""ADR-0027 §2 and Appendix B: switching the sovereign model off for an application blocks its confidential,
restricted and sovereign-only requests (AIPEL-ROUTE-001). They are never rerouted to another model, and the
draft says so before it is published."""

from sqlalchemy import select

from aiva_gateway.db.models import ModelAttemptRow, UsageLedgerRow
from tests.conftest import ADMIN, chat_body

APP = "gct-support-chatbot"
SOVEREIGN = "gct-qwen-finetuned"


async def test_switching_the_sovereign_model_off_blocks_never_reroutes(
    services, make_client, user_token, app_client, db, policy_versions
) -> None:  # type: ignore[no-untyped-def]
    async with make_client(user_token(ADMIN)) as client:
        response = await client.post(
            "/v1/admin/policies/model-switch",
            json={"application_id": APP, "model_id": SOVEREIGN, "enabled": False},
        )
    body = response.json()
    assert response.status_code == 201, body
    checks = {c["classification"]: c for c in body["checks"]}
    for level in ("confidential", "restricted", "sovereign-only"):
        assert checks[level]["sovereign_path"] and checks[level]["blocked"], level
        assert checks[level]["allowed_models_before"] == [SOVEREIGN]
        assert checks[level]["allowed_models_after"] == []
    assert not checks["internal"]["blocked"] and SOVEREIGN not in checks["internal"]["allowed_models_after"]
    assert body["sovereign_blocked"] is True and body["sovereign_blocked_before"] is False
    assert body["warnings"] and "never rerouted" in body["warnings"][0]
    async with make_client(user_token(ADMIN)) as client:
        published = await client.post(
            "/v1/admin/policies/publish",
            json={"version": body["draft"]["version"], "approval_note": "sovereign model maintenance"},
        )
    assert published.status_code == 200, published.json()

    payload = chat_body(
        "Summarise the synthetic ticket",
        data_classification="confidential",
        routing={"mode": "pinned", "tags": ["claude-preferred"], "preferred_model": "claude-flagship"},
    )
    async with app_client(APP) as client:
        blocked = (await client.post("/v1/ai/chat", json=payload)).json()
        internal = (await client.post("/v1/ai/chat", json=chat_body("Explain roaming charges."))).json()
    assert blocked["status"] == "blocked" and "AIPEL-ROUTE-001" in blocked["error"]["rule_ids"]
    assert blocked["selected_model"] is None
    attempts = (
        await db.execute(select(ModelAttemptRow).where(ModelAttemptRow.run_id == blocked["run_id"]))
    ).all()
    ledger = (
        await db.execute(select(UsageLedgerRow).where(UsageLedgerRow.run_id == blocked["run_id"]))
    ).all()
    assert attempts == [] and ledger == []
    # Internal requests keep the application's other models.
    assert internal["status"] == "completed" and internal["selected_model"] != SOVEREIGN
    # The policy_versions fixture restores the starting version afterwards.
