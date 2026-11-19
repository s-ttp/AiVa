"""Scenario 3 — Telecom Fraud Management (LLD §15).

Primary proof: evidence chronology, confidential direct path, no punitive autonomous action.

- Confidential direct path: a confidential investigation with a Claude-preferred hint and tag goes to
  GCT-Qwen on the sovereign direct path, with zero CIM and zero external calls (the LLD §16
  confidentiality benchmark).
- Evidence chronology: every event reaches the sovereign model unmodified, under its own ID, with its
  timestamp and source. No timestamp is collapsed or dropped.
- No punitive autonomous action: the fraud application holds no tools. Suspending a line is denied by
  policy for this run; it is a decision for a human analyst.
"""

from sqlalchemy import select

from aiva_gateway.db.models import AuditLogRow, ModelAttemptRow, PolicyEventRow
from tests.conftest import console
from tests.scenarios.conftest import EXTERNAL, capturing, scenario_fixture

APP = "gct-fraud-investigator"


async def test_sim_swap_investigation_stays_sovereign_with_chronology_intact(
    scenario_services, tester_client, db
) -> None:  # type: ignore[no-untyped-def]
    fixture = scenario_fixture("fraud-management")
    svc, captured = capturing(scenario_services)
    async with tester_client(svc=svc) as client:
        body = (await client.post("/v1/ai/chat", json={**fixture["request"], **console(APP)})).json()

    assert body["status"] == "completed", body.get("error")
    assert body["policy"]["effective_classification"] == "confidential"
    assert "GCT-CONF-001" in body["policy"]["matched_rule_ids"]
    assert body["execution_path"] == "sovereign-direct" and body["selected_model"] == "gct-qwen-finetuned"
    assert (
        not body["cim_invoked"] and body["cim"]["message"] == "CIM bypassed by mandatory sovereignty policy"
    )
    # The Claude-preferred hint and tag never widen access (LLD §7: tags are hints only).
    assert {c["model_id"] for c in body["policy"]["denied_candidates"]} >= EXTERNAL
    attempts = (
        (await db.execute(select(ModelAttemptRow).where(ModelAttemptRow.run_id == body["run_id"])))
        .scalars()
        .all()
    )
    assert [(a.stage, a.model_id) for a in attempts] == [("upstream", "gct-qwen-finetuned")]
    assert body["usage"]["cim_input_tokens"] == 0
    # Output DLP is mandatory on the sovereign path, and the decision is audited at the enhanced level.
    assert body["assurance"]["output_dlp"] in {"clean", "redacted"}

    # Chronology: every event reached the sovereign model unmodified, under its own ID. No timestamp was
    # collapsed or dropped (the Context Guard's chronology rule, held here by the full context).
    [request] = captured.requests
    payload = "\n".join(m["content"] for m in request.messages)
    events = fixture["request"]["evidence"]
    for event in events:
        assert event["text"] in payload and f'id="{event["id"]}"' in payload
    stamps = [line for e in events for line in e["text"].splitlines() if line.startswith("event_timestamp")]
    assert len(set(stamps)) == len(events) and all(payload.count(s) == 1 for s in stamps)


async def test_no_punitive_action_without_a_human(scenario_services, db) -> None:  # type: ignore[no-untyped-def]
    from aiva_gateway.auth.principal import RunIdentity
    from aiva_gateway.contracts.chat import ChatRequest
    from aiva_gateway.core.enums import IdentityMode, PolicyAction, SourceMode
    from aiva_gateway.pipeline.orchestrator import Orchestrator
    from aiva_gateway.pipeline.recorder import persist_run
    from tests.conftest import PEERS

    fixture = scenario_fixture("fraud-management")
    orchestrator = Orchestrator(scenario_services)
    identity = RunIdentity(
        application_id=APP,
        identity_mode=IdentityMode.AUTHENTICATED,
        source_ip=PEERS[APP],
        source_mode=SourceMode.OBSERVED,
        principal_kind="application",
        principal_pseudonym=None,
        roles=(),
    )
    ctx = orchestrator.new_context(ChatRequest.model_validate(fixture["request"]), identity, "gct")
    spans = await orchestrator.run(ctx)
    suspend = await orchestrator.authorize_tool_call(ctx, "subscriber.suspend")
    assert suspend.action == PolicyAction.DENY
    assert "AIPEL-TOOL-004" in {r.rule_id for r in suspend.reasons}  # not granted to the fraud application
    async with scenario_services.sessionmaker() as session, session.begin():
        await persist_run(session, scenario_services.catalog, ctx, spans)
    event = (
        await db.execute(
            select(PolicyEventRow).where(PolicyEventRow.run_id == ctx.run_id, PolicyEventRow.stage == "tool")
        )
    ).scalar_one()
    assert (event.tool_id, event.action) == ("subscriber.suspend", "deny")
    assert (
        await db.execute(select(AuditLogRow).where(AuditLogRow.target_id == ctx.run_id))
    ).first() is None  # the request itself was allowed; only the tool was refused
