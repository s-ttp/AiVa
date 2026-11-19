"""End-to-end behaviour of POST /v1/ai/chat on the P0 placeholder stages."""

from sqlalchemy import select

from aiva_gateway.db.models import ExecutionRunRow, ModelAttemptRow, PolicyEventRow, TraceSpanRow
from aiva_gateway.telemetry.tracing import STAGES
from tests.conftest import chat_body, console

NOC_EVIDENCE = [
    {
        "id": "alarm-1",
        "text": "alarm_id: ALM-7781\nsite_id: RUH-0412\nraised_at: 2026-10-14T06:02:11Z\nseverity: major",
    },
    {
        "id": "alarm-2",
        "text": "alarm_id: ALM-7790\nsite_id: RUH-0419\nraised_at: 2026-10-14T06:03:40Z\nseverity: critical",
    },
]


async def test_internal_question_takes_local_fast_path(app_client, db) -> None:
    async with app_client("gct-support-chatbot") as client:
        response = await client.post("/v1/ai/chat", json=chat_body("What are the Europe roaming rates?"))
    body = response.json()
    assert response.status_code == 200, body
    assert body["status"] == "completed"
    assert body["execution_path"] == "local-fast"
    assert body["cim"]["message"] == "CIM skipped: context sufficient"
    assert body["response"]["provenance"] == "simulated"
    assert body["usage"]["provenance"] == "simulated"
    run = await db.get(ExecutionRunRow, body["run_id"])
    assert run is not None and run.trace_id == body["trace_id"] and run.provenance == "simulated"


async def test_cim_assisted_path_with_verified_context(tester_client, db) -> None:
    payload = chat_body(
        "Correlate the alarms.",
        scenario_id="network-operations",
        # A fact-free note the CIM may omit; with alarms alone there would be nothing to omit (CIM bypassed).
        evidence=[*NOC_EVIDENCE, {"id": "note-1", "text": "Synthetic shift note: the review moved."}],
        trace=True,
        **console("gct-noc-assistant"),
    )
    async with tester_client() as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["execution_path"] == "cim-assisted"
    assert body["cim_invoked"] is True
    assert body["cim"]["sufficiency"] == "verified"
    assert body["identity"] == {
        "application_id": "gct-noc-assistant",
        "identity_mode": "simulated",
        "source_mode": "simulated",
        "matched_network_id": "net-test-console",
    }
    stages = [s["name"] for s in body["trace"] if s["name"] != "aiva.chat"]
    assert stages == list(STAGES)
    assert all(s["status"] == "executed" for s in body["trace"] if s["name"] in STAGES)
    attempts = (
        await db.execute(select(ModelAttemptRow).where(ModelAttemptRow.run_id == body["run_id"]))
    ).scalars()
    assert [a.stage for a in sorted(attempts, key=lambda a: a.sequence)] == ["cim", "upstream"]


async def test_failed_guard_restores_full_context(tester_client) -> None:
    evidence = [{"id": "alarm-1", "text": "Free text without the required structured fields"}]
    payload = chat_body(
        "Correlate the alarms.",
        scenario_id="network-operations",
        evidence=evidence,
        **console("gct-noc-assistant"),
    )
    async with tester_client() as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed"
    assert body["cim"]["sufficiency"] == "failed-fallback-applied"
    assert body["cim"]["message"] == "Context sufficiency check failed; safe fallback applied"
    assert body["routing"]["context_strategy"] == "full"


async def test_every_run_emits_all_stage_spans(app_client, db) -> None:
    async with app_client("gct-support-chatbot", peer="203.0.113.9") as client:
        body = (await client.post("/v1/ai/chat", json=chat_body("hello"))).json()
    assert body["status"] == "blocked"
    spans = (
        (await db.execute(select(TraceSpanRow).where(TraceSpanRow.run_id == body["run_id"]))).scalars().all()
    )
    by_name = {s.name: s for s in spans}
    assert set(STAGES) <= by_name.keys()
    assert by_name["policy"].status == "executed"
    assert by_name["inference"].status == "skipped"
    assert by_name["inference"].attributes["aiva.stage.skip_reason"] == "Blocked by AI policy"


async def test_idempotent_replay_and_conflict(app_client, db) -> None:
    headers = {"Idempotency-Key": "test-idem-001"}
    async with app_client("gct-support-chatbot", headers=headers) as client:
        first = await client.post("/v1/ai/chat", json=chat_body("Roaming rates?"))
        second = await client.post("/v1/ai/chat", json=chat_body("Roaming rates?"))
        conflict = await client.post("/v1/ai/chat", json=chat_body("Something else"))
    assert first.json()["run_id"] == second.json()["run_id"]
    assert second.headers.get("Idempotent-Replayed") == "true"
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    runs = (
        await db.execute(select(ExecutionRunRow).where(ExecutionRunRow.run_id == first.json()["run_id"]))
    ).all()
    assert len(runs) == 1


async def test_policy_events_record_bundle_identity(app_client, db, services) -> None:
    async with app_client("gct-support-chatbot") as client:
        body = (await client.post("/v1/ai/chat", json=chat_body("Roaming rates?"))).json()
    events = (
        (await db.execute(select(PolicyEventRow).where(PolicyEventRow.run_id == body["run_id"])))
        .scalars()
        .all()
    )
    assert {e.stage for e in events} == {"request", "egress"}
    assert all(e.bundle_hash == body["policy"]["bundle_hash"] for e in events)
    assert body["policy"]["policy_version"]


async def test_baseline_comparison_is_metered_separately(app_client) -> None:
    async with app_client("gct-support-chatbot") as client:
        body = (
            await client.post("/v1/ai/chat", json=chat_body("Roaming rates?", baseline_comparison=True))
        ).json()
    assert body["status"] == "completed"
    assert body["cost"]["baseline_micros"] > 0
    assert body["cost"]["upstream_micros"] == 0  # local GCT-Qwen: $0 token price by POC convention


async def test_baseline_denied_for_confidential(tester_client, db) -> None:
    payload = chat_body(
        "Summarise case FR-1.",
        baseline_comparison=True,
        data_classification="confidential",
        **console("gct-fraud-investigator"),
    )
    async with tester_client() as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed"
    assert body["cost"]["baseline_micros"] == 0
    attempts = (
        await db.execute(select(ModelAttemptRow).where(ModelAttemptRow.run_id == body["run_id"]))
    ).scalars()
    assert {a.model_id for a in attempts} == {"gct-qwen-finetuned"}
