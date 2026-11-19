"""Per-request GPU estimate in the API response (LLD §12, ADR-0006): it equals the run's `gpu_estimate` ledger
charges to the micro and carries a label of its own, never the API amounts' label. A replayed call has none:
its latency is the local replay's, not the endpoint's busy time."""

from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import select

from aiva_gateway.catalog.models import ModelSpec
from aiva_gateway.core.enums import ChargeType, Provenance
from aiva_gateway.db.models import UsageLedgerRow
from aiva_gateway.pipeline.interfaces import (
    CimCallResult,
    CimRequest,
    InferenceAdapter,
    InferenceRequest,
    InferenceResult,
)
from tests.conftest import chat_body, with_overrides

# Stands in for a live endpoint: measured usage and a fixed busy time. At $2.50/h (rc-gct-qwen-local)
# 1,440 ms is 1,000 micro-USD; at $0.80/h (rc-enterprise-cim) 4,500 ms is 1,000 micro-USD.
LIVE = Provenance.MEASURED
QWEN_MS, CIM_MS = 1440.0, 4500.0
CONFIDENTIAL = chat_body("Summarise the account notes for this customer.", data_classification="confidential")
CIM_ASSISTED = chat_body(
    "What are the Europe roaming rates?",
    evidence=[
        {"id": "kb-roaming", "text": "Synthetic KB: Europe roaming costs AED 5 per MB in zone 1."},
        {"id": "note-1", "text": "Synthetic office note: the cafeteria menu changes on Sundays."},
    ],
)


class _Timed:
    def __init__(self, inner: InferenceAdapter, label: Provenance) -> None:
        self.inner, self.label = inner, label

    async def complete(self, request: InferenceRequest) -> InferenceResult:
        result = await self.inner.complete(request)
        usage = replace(result.usage, provenance=self.label)
        return replace(result, usage=usage, latency_ms=QWEN_MS, provenance=self.label)


class TimedAdapters:
    def __init__(self, inner: Any, label: Provenance) -> None:
        self.inner, self.label = inner, label

    def for_model(self, model: ModelSpec) -> InferenceAdapter:
        return _Timed(self.inner.for_model(model), self.label)

    async def aclose(self) -> None:
        return None


class TimedCim:
    def __init__(self, inner: Any, label: Provenance) -> None:
        self.inner, self.label = inner, label

    async def package(self, request: CimRequest, model: ModelSpec) -> CimCallResult:
        result: CimCallResult = await self.inner.package(request, model)
        usage = replace(result.usage, provenance=self.label)
        return replace(result, usage=usage, latency_ms=CIM_MS, provenance=self.label)


def timed(services: Any, label: Provenance = LIVE) -> Any:
    """A live gateway (`measured` calls), or one replaying recordings (`replayed`), with fixed busy times."""
    mode = "live" if label is LIVE else "replay"
    settings = services.settings.model_copy(update={"inference_mode": mode})
    return with_overrides(
        services,
        settings=settings,
        adapters=TimedAdapters(services.adapters, label),
        cim=TimedCim(services.cim, label),
    )


async def run(app_client, svc: Any, payload: dict[str, Any]) -> dict[str, Any]:
    async with app_client("gct-support-chatbot", svc=svc) as client:
        return (await client.post("/v1/ai/chat", json=payload)).json()


async def gpu_charges(db, run_id: str) -> list[UsageLedgerRow]:
    rows = await db.scalars(
        select(UsageLedgerRow).where(
            UsageLedgerRow.run_id == run_id, UsageLedgerRow.charge_type == ChargeType.GPU_ESTIMATE.value
        )
    )
    return list(rows)


async def test_sovereign_direct_gpu_estimate_matches_the_ledger(app_client, services, db) -> None:
    body = await run(app_client, timed(services), CONFIDENTIAL)
    assert body["status"] == "completed", body.get("error")
    assert body["execution_path"] == "sovereign-direct" and body["selected_model"] == "gct-qwen-finetuned"
    charges = await gpu_charges(db, body["run_id"])
    cost = body["cost"]
    assert cost["gpu_estimated_micros"] == sum(c.amount_micros for c in charges) == 1000
    assert cost["gpu_estimated_provenance"] == "estimated" == charges[0].measured_or_estimated
    assert cost["provenance"] == "measured" and cost["total_api_micros"] == 0  # $0 token price


async def test_cim_assisted_gpu_estimate_matches_the_ledger(app_client, services, db) -> None:
    body = await run(app_client, timed(services), CIM_ASSISTED)
    assert body["status"] == "completed", body.get("error")
    assert body["execution_path"] == "cim-assisted"
    charges = await gpu_charges(db, body["run_id"])
    assert "rc-enterprise-cim" in {c.rate_card_id for c in charges}
    cost = body["cost"]
    assert cost["gpu_estimated_micros"] == sum(c.amount_micros for c in charges) >= 1000
    assert cost["gpu_estimated_provenance"] == "estimated"
    assert {c.measured_or_estimated for c in charges} == {"estimated"}


@pytest.mark.parametrize("payload", [CONFIDENTIAL, CIM_ASSISTED], ids=["sovereign-direct", "cim-assisted"])
async def test_a_replayed_call_has_no_gpu_estimate(app_client, services, db, payload) -> None:
    # Live, these busy times would be 1,000 micro-USD each; a replay's latency is not the endpoint's.
    body = await run(app_client, timed(services, Provenance.REPLAYED), payload)
    assert body["status"] == "completed", body.get("error")
    assert await gpu_charges(db, body["run_id"]) == []
    cost = body["cost"]
    assert cost["gpu_estimated_micros"] == 0 and cost["gpu_estimated_provenance"] is None
    # The API charges are kept, priced at the rate card and labelled as what they are.
    rows = await db.scalars(select(UsageLedgerRow).where(UsageLedgerRow.run_id == body["run_id"]))
    assert {(row.measured_or_estimated, row.rate_card_version) for row in rows} == {
        ("replayed", cost["rate_card_version"])
    }
    assert cost["provenance"] == "replayed"


async def test_simulated_inference_labels_the_gpu_estimate_simulated(app_client, services, db) -> None:
    body = await run(app_client, services, CONFIDENTIAL)
    assert body["execution_path"] == "sovereign-direct"
    charges = await gpu_charges(db, body["run_id"])
    cost = body["cost"]
    assert cost["gpu_estimated_micros"] == sum(c.amount_micros for c in charges)
    assert cost["gpu_estimated_provenance"] == "simulated"


async def test_no_gpu_label_when_no_call_ran_on_a_gpu_endpoint(app_client, services) -> None:
    body = await run(app_client, timed(services), chat_body("Use key sk-ant-abcdefghijklmnopqrstuvwxyz123"))
    assert body["status"] == "blocked" and body["outcome"] == "policy_denied"
    assert body["cost"]["gpu_estimated_micros"] == 0 and body["cost"]["gpu_estimated_provenance"] is None
