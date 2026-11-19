"""Invariant (ADR-0019): a replay run never opens a network connection, even with live credentials present.

The gateway is configured live. A run in replay mode is answered from the recordings shipped with the
gateway, or, on a miss, by the simulated adapter; either way no DNS lookup or connection leaves the host.
"""

from sqlalchemy import select

from aiva_gateway.db.models import ExecutionRunRow, ModelAttemptRow, UsageLedgerRow
from tests.conftest import LIVE_SETTINGS, chat_body, console
from tests.live.test_endpoint_pipeline import scenario_request


async def test_a_replay_run_never_opens_a_network_connection(
    live_services, tester_client, db, outbound
) -> None:  # type: ignore[no-untyped-def]
    assert live_services.settings.inference_mode == "live" and LIVE_SETTINGS["hf_token"]
    # Recorded: scenario 3 on the sovereign fine-tune (Endpoint B), at the URL it was recorded against. The
    # HF Endpoints API is not called either.
    recorded = {
        **scenario_request("fraud-management"),
        "execution_mode": "replay",
        **console("gct-fraud-investigator"),
    }
    unrecorded = chat_body(
        "A free-text question nobody recorded.", execution_mode="replay", **console("gct-support-chatbot")
    )
    async with tester_client(svc=live_services) as client:
        hit = (await client.post("/v1/ai/chat", json=recorded)).json()
        miss = (await client.post("/v1/ai/chat", json=unrecorded)).json()
    for body in (hit, miss):
        assert body["status"] == "completed", body.get("error")
        assert body["execution_mode"] == "replay"
    assert hit["execution_path"] == "sovereign-direct" and hit["selected_model"] == "gct-qwen-finetuned"
    assert hit["response"]["provenance"] == "replayed"
    assert miss["response"]["provenance"] == "simulated"  # never shown as measured or replayed
    for body, provenance in ((hit, "replayed"), (miss, "simulated")):
        attempts = (
            await db.execute(
                select(ModelAttemptRow).where(
                    ModelAttemptRow.run_id == body["run_id"], ModelAttemptRow.stage == "upstream"
                )
            )
        ).scalars()
        assert {a.provenance for a in attempts} == {provenance}
    # Money labels (ADR-0006, FinOps' illustrative domain): the hold is made in replay mode before the call
    # shows whether it was recorded, so both runs reserve `replayed`. The miss's charges carry its attempt's
    # label, and the run's weakest label is `simulated`: nothing reads `measured`.
    assert hit["budget"]["provenance"] == miss["budget"]["provenance"] == "replayed"
    for body, provenance in ((hit, "replayed"), (miss, "simulated")):
        run = (
            await db.execute(select(ExecutionRunRow).where(ExecutionRunRow.run_id == body["run_id"]))
        ).scalar_one()
        assert run.provenance == provenance
        labels = await db.scalars(
            select(UsageLedgerRow.measured_or_estimated)
            .distinct()
            .where(
                UsageLedgerRow.run_id == body["run_id"], UsageLedgerRow.charge_type == "upstream_inference"
            )
        )
        assert set(labels) == {provenance}
    assert outbound == []
