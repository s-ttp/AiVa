"""Endpoints A/B through `POST /v1/ai/chat` on recorded exchanges (the Optimize & Inference exit gate).

- Scenario 3 (fraud): a confidential investigation takes the sovereign direct path to the GCT-Qwen
  fine-tune on Endpoint B. No CIM and no external call; the endpoint's GPU time is a FinOps estimate.
- NOC: the CIM fine-tune on Endpoint A packages a small alarm set, the Context Guard verifies the package,
  and only then does the upstream model receive it. At scenario 5's full size (60 alarms, each carrying
  facts found nowhere else) nothing can be omitted, so the CIM is bypassed and the full context is used.

Recorded with `AIVA_RECORD_PROVIDERS=1` against the woken endpoints (see tests/live/conftest.py).
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from aiva_gateway.config import REPO_ROOT, Settings
from aiva_gateway.db.models import ModelAttemptRow, UsageLedgerRow
from aiva_gateway.services import Services, build_services
from tests.conftest import chat_body, console
from tests.live.conftest import (
    ENDPOINT_NAMESPACE,
    ENDPOINT_URLS,
    EXPECTED_PROVENANCE,
    MODE,
    RECORDING,
    RECORDINGS_DIR,
)

SCENARIOS = Path(REPO_ROOT, "gateway", "tests", "fixtures", "scenarios")
NOC_QUESTION = "Correlate the synthetic alarms from cell sites RUH-0412 and RUH-0419."
NOC_EVIDENCE = [
    {
        "id": "noc-1",
        "text": "alarm_id: A-7781\nsite_id: RUH-0412\nraised_at: 2026-10-14T06:02:11Z\nseverity: major\n"
        "Link down on microwave hop MW-12.",
    },
    {
        "id": "noc-2",
        "text": "alarm_id: A-7790\nsite_id: RUH-0419\nraised_at: 2026-10-14T06:03:40Z\nseverity: minor\n"
        "High VSWR on sector 2.",
    },
    {"id": "mkt-1", "text": "Synthetic marketing note: the Traveller 10 bundle launches in November."},
]


def scenario_request(name: str) -> dict[str, Any]:
    return dict(json.loads((SCENARIOS / f"{name}.json").read_text())["request"])


@pytest.fixture(scope="module")
async def endpoint_services(services: Services, settings: Settings) -> AsyncIterator[Services]:
    endpoints: dict[str, Any] = (
        {"endpoint_namespace": ENDPOINT_NAMESPACE} if RECORDING else {"endpoint_urls": ENDPOINT_URLS}
    )
    live = settings.model_copy(
        update={
            "inference_mode": MODE,
            "recordings_dir": RECORDINGS_DIR,
            "retrieval_enabled": False,
            "response_cache_enabled": False,
            "simulated_models": [],
            **endpoints,
        }
    )
    svc = build_services(live)
    yield svc
    await svc.aclose()


def expected_version(svc: Services, model_id: str) -> str:
    spec = svc.catalog.model(model_id)
    assert spec is not None
    return spec.pinned_version if RECORDING else str(spec.adapter)


async def _attempts(db, run_id: str) -> list[ModelAttemptRow]:  # type: ignore[no-untyped-def]
    rows = await db.execute(select(ModelAttemptRow).where(ModelAttemptRow.run_id == run_id))
    return sorted(rows.scalars().all(), key=lambda a: a.sequence)


async def test_confidential_fraud_run_on_the_sovereign_fine_tune(
    endpoint_services, tester_client, db
) -> None:  # type: ignore[no-untyped-def]
    payload = {**scenario_request("fraud-management"), **console("gct-fraud-investigator")}
    async with tester_client(svc=endpoint_services) as client:
        response = await client.post("/v1/ai/chat", json=payload)
    body = response.json()
    assert response.status_code == 200, body.get("error")
    assert body["status"] == "completed" and body["execution_path"] == "sovereign-direct"
    assert body["selected_model"] == "gct-qwen-finetuned" and not body["cim_invoked"]
    assert body["response"]["provenance"] == EXPECTED_PROVENANCE.value
    assert "FR-2026-0412" in body["response"]["content"]

    [attempt] = await _attempts(db, body["run_id"])  # no CIM, no external model
    assert (attempt.stage, attempt.model_id, attempt.status) == ("upstream", "gct-qwen-finetuned", "ok")
    assert attempt.provider_model_version == expected_version(endpoint_services, "gct-qwen-finetuned")
    assert attempt.provenance == EXPECTED_PROVENANCE.value
    assert attempt.input_tokens > 0 and attempt.output_tokens > 0 and attempt.reasoning_tokens == 0
    assert attempt.ttft_ms is not None

    ledger = (
        (await db.execute(select(UsageLedgerRow).where(UsageLedgerRow.run_id == body["run_id"])))
        .scalars()
        .all()
    )
    charges = {row.charge_type: row for row in ledger}
    assert charges["upstream_inference"].amount_micros == 0  # a dedicated endpoint has no per-token price
    if RECORDING:
        gpu = charges["gpu_estimate"]  # its GPU time, estimated by FinOps from the rate card
        assert gpu.amount_micros > 0 and gpu.measured_or_estimated == "estimated"
    # A replay's latency is the local replay's, not the endpoint's busy time: no GPU estimate.
    assert set(charges) == ({"upstream_inference", "gpu_estimate"} if RECORDING else {"upstream_inference"})


async def test_small_noc_package_from_the_cim_fine_tune_is_verified_before_upstream(
    endpoint_services, tester_client, db
) -> None:  # type: ignore[no-untyped-def]
    payload = chat_body(
        NOC_QUESTION,
        task="analysis",
        evidence=NOC_EVIDENCE,
        scenario_id="network-operations",  # its context profile: network-alarm-correlation
        routing={"mode": "pinned", "preferred_model": "gemini-flash-lite"},
        max_output_tokens=300,
        **console("gct-noc-assistant"),
    )
    async with tester_client(svc=endpoint_services) as client:
        response = await client.post("/v1/ai/chat", json=payload)
    body = response.json()
    assert response.status_code == 200, body.get("error")
    assert body["status"] == "completed" and body["cim_invoked"]
    assert body["cim"]["sufficiency"] == "verified", body["cim"]
    assert body["response"]["provenance"] == EXPECTED_PROVENANCE.value

    attempts = await _attempts(db, body["run_id"])
    assert [(a.stage, a.model_id, a.status) for a in attempts] == [
        ("cim", "enterprise-cim", "ok"),
        ("upstream", "gemini-flash-lite", "ok"),
    ]
    cim = attempts[0]
    assert cim.provenance == EXPECTED_PROVENANCE.value and cim.input_tokens > 0 and cim.output_tokens > 0
    assert cim.provider_model_version == expected_version(endpoint_services, "enterprise-cim")
    assert body["cim"]["cim_revision"] == cim.provider_model_version
    # The unrelated marketing passage was left out; both alarms reached the model under their ids.
    assert body["usage"]["upstream_submitted_input_tokens"] > 0


async def test_full_noc_scenario_bypasses_the_cim_when_nothing_can_be_omitted(
    endpoint_services, tester_client, db
) -> None:  # type: ignore[no-untyped-def]
    """Scenario 5 at full size: each of the 60 alarms carries required facts found in no other passage, so
    the Context Guard could only verify the full context. The CIM is not called (ML measured the only
    passing package at 6,260 output tokens, about 80 s on Endpoint A, for no saving); the model receives
    the full eligible context, as the earlier fallback did."""
    payload = {**scenario_request("network-operations"), **console("gct-noc-assistant")}
    async with tester_client(svc=endpoint_services) as client:
        response = await client.post("/v1/ai/chat", json=payload)
    body = response.json()
    assert response.status_code == 200, body.get("error")
    assert body["status"] == "completed" and not body["cim_invoked"]
    assert body["cim"]["message"] == "CIM bypassed: nothing to omit; full context used"
    assert body["cim"]["sufficiency"] == "not-applicable"
    attempts = await _attempts(db, body["run_id"])
    assert [(a.stage, a.status) for a in attempts] == [("upstream", "ok")]
    # The full context reached the model: every one of the 60 alarms is in the submitted payload.
    assert body["usage"]["upstream_submitted_input_tokens"] >= body["usage"]["original_context_tokens"]
