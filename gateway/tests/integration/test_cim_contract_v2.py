"""A v2 CIM through the pipeline (ADR-0018): the CIM selects, the gateway builds the package, the Context
Guard verifies it as under v1, and the trace records the contract."""

import json
from dataclasses import replace

from sqlalchemy import select

from aiva_gateway.db.models import TraceSpanRow
from aiva_gateway.inference.registry import AdapterRegistry
from aiva_gateway.services import Services
from tests.conftest import chat_body, console, with_overrides
from tests.integration.test_context_sufficiency import ALARMS, REQUIRED, CapturingAdapter
from tests.unit.test_cim_contract_v2 import v2_cim

SELECTION = {
    "intent": "correlate alarms",
    "required_facts": [
        {"field": field, "value": value, "source_id": evidence_id}
        for evidence_id in ("alarm-1", "alarm-2")
        for field, value in (line.split(": ", 1) for line in ALARMS[evidence_id].splitlines() if ": " in line)
        if field in REQUIRED
    ],
    "selected_evidence_ids": ["alarm-1", "alarm-2"],
    "omitted_evidence_ids": ["note-1"],
    "missing_fields": [],
    "self_reported_complete": True,
}


def with_v2_cim(services: Services, sent: list[dict[str, object]], **cim_spec: object) -> Services:
    models = services.catalog.models
    update = {"cim_contract": "v2", **cim_spec}
    cim = [m.model_copy(update=update) if m.role == "cim" else m for m in models.models]
    catalog = replace(services.catalog, models=models.model_copy(update={"models": cim}))
    return with_overrides(services, catalog=catalog, cim=v2_cim(json.dumps(SELECTION), sent))


async def test_a_v2_selection_is_packaged_by_the_gateway_and_verified(services, tester_client, db) -> None:  # type: ignore[no-untyped-def]
    sent: list[dict[str, object]] = []
    adapter = CapturingAdapter()
    svc = with_overrides(
        with_v2_cim(services, sent), adapters=AdapterRegistry(services.catalog, adapters={}, default=adapter)
    )
    payload = chat_body(
        "Correlate the alarms.",
        scenario_id="network-operations",
        evidence=[{"id": k, "text": v} for k, v in ALARMS.items()],
        **console("gct-noc-assistant"),
    )
    async with tester_client(svc=svc) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed", body.get("error")
    assert len(sent) == 1 and body["cim"]["sufficiency"] == "verified"  # the Guard passed the built package
    [upstream] = adapter.payloads
    assert ALARMS["alarm-1"] in upstream and ALARMS["alarm-2"] in upstream
    assert ALARMS["note-1"] not in upstream  # omitted by the CIM, so not in the package the gateway built
    spans = {
        span.name: span.attributes
        for span in (
            await db.execute(select(TraceSpanRow).where(TraceSpanRow.run_id == body["run_id"]))
        ).scalars()
    }
    cim_span = next(attributes for attributes in spans.values() if "aiva.cim.revision" in attributes)
    assert cim_span["aiva.cim.contract"] == "v2"


async def test_the_v2_decode_rate_decides_the_time_bypass(services, tester_client, db) -> None:  # type: ignore[no-untyped-def]
    # Eight alarms (32 facts, about 1.5k reply tokens): about 50 s at 29 tok/s, over the 48 s budget, so the
    # CIM is not called; at the measured v2 default of 40 tok/s (about 37 s) it is.
    alarms = {
        f"alarm-{i}": f"alarm_id: ALM-{7700 + i}\nsite_id: RUH-{400 + i:04d}\n"
        f"raised_at: 2026-10-14T06:{i:02d}:00Z\nseverity: major"
        for i in range(1, 9)
    }
    evidence = [{"id": k, "text": v} for k, v in {**alarms, "note-1": ALARMS["note-1"]}.items()]
    payload = chat_body(
        "Correlate the alarms.",
        scenario_id="network-operations",
        evidence=evidence,
        **console("gct-noc-assistant"),
    )
    for rate, called in ((29.0, False), (None, True)):
        sent: list[dict[str, object]] = []
        svc = with_v2_cim(services, sent)
        if rate is not None:
            svc = with_overrides(
                svc, settings=svc.settings.model_copy(update={"cim_v2_decode_tokens_per_s": rate})
            )
        async with tester_client(svc=svc) as client:
            body = (await client.post("/v1/ai/chat", json=payload)).json()
        assert body["status"] == "completed", body.get("error")
        assert bool(sent) is called, rate
        bypass = await db.scalar(
            select(TraceSpanRow.attributes["aiva.cim.bypass_reason"].astext).where(
                TraceSpanRow.run_id == body["run_id"],
                TraceSpanRow.attributes.has_key("aiva.cim.bypass_reason"),
            )
        )
        assert bypass == (None if called else "too-large-to-build-in-time"), rate
        # Bypassed or not, retrieval ran over every passage; only a package has kept and omitted counts.
        cim = body["cim"]
        assert cim["passages_retrieved"] == len(evidence), rate
        assert (cim["passages_kept"] is None) is not called, rate


async def test_a_task_the_cim_was_not_trained_on_bypasses_it(services, tester_client, db) -> None:  # type: ignore[no-untyped-def]
    # v6 is trained on qa, rag, agentic, summarization and analysis, never on coding (ML & Data).
    trained = ["qa", "rag", "agentic", "summarization", "analysis"]
    evidence = [{"id": k, "text": v} for k, v in ALARMS.items()]
    cases = (
        ("coding", "coding-assistant", "gct-coding-assistant", False),
        ("analysis", "network-operations", "gct-noc-assistant", True),
    )
    for task, scenario, application, called in cases:
        sent: list[dict[str, object]] = []
        svc = with_v2_cim(services, sent, cim_trained_tasks=trained)
        payload = chat_body(
            "Correlate the alarms.",
            task=task,
            scenario_id=scenario,
            evidence=evidence,
            **console(application),
        )
        async with tester_client(svc=svc) as client:
            body = (await client.post("/v1/ai/chat", json=payload)).json()
        assert body["status"] == "completed", body.get("error")
        assert bool(sent) is called, task
        if not called:
            assert (
                body["cim"]["message"]
                == "CIM bypassed: task outside the CIM's trained categories; full context used"
            )
