"""Internet key calls and the in-house GPU endpoints (ADR-0027 §3, owner decision 2026-10-26).

An application key admitted from the internet never wakes a scaled-to-zero endpoint, as a public visitor's
live run never does (ADR-0019): a sleeping CIM is bypassed, and a sleeping upstream, the sovereign one
included, refuses the run with nothing reserved or metered and is never rerouted. Its live runs' estimated
GPU cost is capped by the tenant's `internet_gpu` budget, a second limit beside the applications' API
budgets. A key called from a trusted network, a user and a public visitor are unchanged.
"""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from aiva_gateway.core.enums import Provenance
from aiva_gateway.db.models import BudgetRow, ModelAttemptRow, UsageLedgerRow
from aiva_gateway.finops.budgets import budget_id
from aiva_gateway.finops.scopes import INTERNET_GPU, LIVE, Bucket, period_key
from aiva_gateway.inference.registry import AdapterRegistry
from aiva_gateway.pipeline.interfaces import InferenceRequest, InferenceResult
from aiva_gateway.services import Services
from tests.conftest import CONSOLE_IP, chat_body, console, with_overrides
from tests.integration.test_execution_mode import Asleep, NoCim, Spy
from tests.integration.test_money_domains import Labelled, spent
from tests.integration.test_public_demo import proxied, through_proxy

SANDBOX = "gct-api-sandbox"
QUESTION = "Explain Python list slicing in two sentences."
CONFIDENTIAL = {"task": "summarization", "data_classification": "confidential"}
EVIDENCE = [
    {
        "id": "doc-1",
        "text": "Synthetic style guide: slices copy; s[a:b] runs from a up to, not including, b.",
    },
    {"id": "doc-2", "text": "Synthetic shift note: the weekly dashboard review moved to Tuesday."},
]
GPU_CAP = 5_000_000  # $5.00 a month (config/tenant.yaml)


class Busy(Spy):
    """The spy upstream with a fixed 12 s call, so its GPU estimate is not rounded to $0."""

    async def complete(self, request: InferenceRequest) -> InferenceResult:
        return replace(await super().complete(request), latency_ms=12_000.0)


def internet_gpu_bucket() -> Bucket:
    return Bucket(INTERNET_GPU, "gct", "monthly", period_key("monthly", datetime.now(UTC)), LIVE)


async def set_internet_gpu_cap(services: Services, cap: int) -> None:
    async with services.sessionmaker() as session, session.begin():
        statement = insert(BudgetRow).values(
            id=budget_id(INTERNET_GPU, "gct", "monthly"),
            tenant_id="gct",
            scope_type=INTERNET_GPU,
            scope_id="gct",
            period="monthly",
            cap_micros=cap,
            alert_thresholds=[0.8, 0.95],
            on_exhaustion="block",
        )
        await session.execute(
            statement.on_conflict_do_update(constraint="uq_budget_tenant_id", set_={"cap_micros": cap})
        )
    await services.quota.publish_budgets("gct")


@pytest.fixture
async def restore_gpu_cap(services: Services) -> AsyncIterator[None]:
    yield
    await set_internet_gpu_cap(services, GPU_CAP)


def deployed(
    live_services: Services,
    awake: frozenset[str] = frozenset(),
    *,
    measured: bool = False,
    proxy: bool = True,
) -> tuple[Services, Spy, Asleep]:
    """The live gateway, behind the platform proxy (Space 2) unless `proxy` is false, whose endpoints are
    asleep except `awake`. `measured` labels the simulated upstream as a live provider, so its GPU estimate
    is money."""
    spy, asleep = Busy(), Asleep(awake)
    adapters: Any = AdapterRegistry(live_services.catalog, adapters={}, default=spy)
    svc = with_overrides(
        proxied(live_services) if proxy else live_services,
        adapters=Labelled(adapters, Provenance.MEASURED) if measured else adapters,
        resolver=asleep,
        cim=NoCim(),
    )
    return svc, spy, asleep


async def _attempts(db, run_id: str) -> list[ModelAttemptRow]:  # type: ignore[no-untyped-def]
    return list((await db.scalars(select(ModelAttemptRow).where(ModelAttemptRow.run_id == run_id))).all())


async def test_a_sleeping_sovereign_endpoint_refuses_an_internet_key_call(
    live_services, make_client, credentials, db
) -> None:  # type: ignore[no-untyped-def]
    # Confidential data never goes to an awake external model: the run fails closed while Endpoint B sleeps.
    svc, spy, asleep = deployed(live_services)
    async with make_client(svc=svc, headers=through_proxy(credentials[SANDBOX])) as client:
        body = (await client.post("/v1/ai/chat", json=chat_body(QUESTION, **CONFIDENTIAL))).json()
    assert body["identity"]["matched_network_id"] == "internet"
    assert body["execution_path"] == "sovereign-direct"
    assert body["status"] == "blocked" and body["outcome"] == "error"
    assert body["error"]["code"] == "LIVE_DEMO_UNAVAILABLE" and not body["error"]["retryable"]
    assert body["error"]["details"] == {"reason": "endpoint_cold", "resets_at": None}
    assert asleep.checked == ["gct-qwen-finetuned"]
    assert spy.called == [] and await _attempts(db, body["run_id"]) == []  # never rerouted, never called
    assert body["budget"]["reserved_micros"] == body["budget"]["committed_micros"] == 0


async def test_a_sleeping_cim_is_bypassed_on_an_internet_key_call(
    live_services, make_client, credentials
) -> None:  # type: ignore[no-untyped-def]
    svc, spy, asleep = deployed(live_services)
    payload = chat_body(
        QUESTION, task="qa", evidence=EVIDENCE, routing={"preferred_model": "gemini-flash-lite"}
    )
    async with make_client(svc=svc, headers=through_proxy(credentials[SANDBOX])) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed", body.get("error")
    assert body["execution_path"] == "cim-assisted" and not body["cim_invoked"]  # NoCim would have raised
    assert (
        body["cim"]["message"]
        == "CIM bypassed: the CIM endpoint is asleep and is not woken; full context used"
    )
    assert "enterprise-cim" in asleep.checked
    assert spy.called and "gct-qwen-finetuned" not in spy.called


async def test_a_sleeping_b_refuses_an_internet_key_call_before_the_cim(
    live_services, make_client, credentials, db
) -> None:  # type: ignore[no-untyped-def]
    # The CIM endpoint is awake: only the up-front check of the planned path keeps the CIM from spending.
    svc, spy, asleep = deployed(live_services, awake=frozenset({"enterprise-cim"}))
    payload = chat_body(
        QUESTION, task="qa", evidence=EVIDENCE, routing={"preferred_model": "gct-qwen-finetuned"}
    )
    async with make_client(svc=svc, headers=through_proxy(credentials[SANDBOX])) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "blocked" and body["error"]["details"]["reason"] == "endpoint_cold"
    assert asleep.checked == ["gct-qwen-finetuned"]
    assert not body["cim_invoked"] and spy.called == [] and await _attempts(db, body["run_id"]) == []


async def test_a_key_call_from_a_trusted_network_still_wakes_endpoints(
    live_services, make_client, credentials
) -> None:  # type: ignore[no-untyped-def]
    svc, spy, asleep = deployed(live_services, proxy=False)
    async with make_client(credentials[SANDBOX], svc=svc, peer=CONSOLE_IP) as client:
        body = (await client.post("/v1/ai/chat", json=chat_body(QUESTION, **CONFIDENTIAL))).json()
    assert body["identity"]["matched_network_id"] == "net-test-console"
    assert body["status"] == "completed", body.get("error")
    assert asleep.checked == [] and spy.called == ["gct-qwen-finetuned"]


async def test_a_users_test_console_run_still_wakes_endpoints(live_services, make_client, user_token) -> None:  # type: ignore[no-untyped-def]
    svc, spy, asleep = deployed(live_services)
    payload = chat_body(QUESTION, **CONFIDENTIAL, **console(SANDBOX))
    async with make_client(svc=svc, headers=through_proxy(user_token("u-tester"))) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed", body.get("error")
    assert asleep.checked == [] and spy.called == ["gct-qwen-finetuned"]


async def test_the_internet_gpu_budget_counts_estimated_gpu_cost_only(
    live_services, make_client, credentials, db
) -> None:  # type: ignore[no-untyped-def]
    svc, spy, _ = deployed(live_services, awake=frozenset({"gct-qwen-finetuned"}), measured=True)
    before = await spent(svc, internet_gpu_bucket())
    async with make_client(svc=svc, headers=through_proxy(credentials[SANDBOX])) as client:
        body = (await client.post("/v1/ai/chat", json=chat_body(QUESTION, **CONFIDENTIAL))).json()
    assert body["status"] == "completed", body.get("error")
    assert spy.called == ["gct-qwen-finetuned"]
    gpu = body["cost"]["gpu_estimated_micros"]
    assert gpu > 0 and body["cost"]["gpu_estimated_provenance"] == "estimated"  # never measured
    assert await spent(svc, internet_gpu_bucket()) - before == gpu  # the GPU estimate, no API spend
    labels = await db.scalars(
        select(UsageLedgerRow.measured_or_estimated).where(
            UsageLedgerRow.run_id == body["run_id"], UsageLedgerRow.charge_type == "gpu_estimate"
        )
    )
    assert set(labels) == {"estimated"}


async def test_the_internet_gpu_budget_blocks_at_its_cap(
    live_services, make_client, credentials, db, restore_gpu_cap
) -> None:  # type: ignore[no-untyped-def]
    await set_internet_gpu_cap(live_services, 1)
    svc, spy, _ = deployed(live_services, awake=frozenset({"gct-qwen-finetuned"}), measured=True)
    async with make_client(svc=svc, headers=through_proxy(credentials[SANDBOX])) as client:
        sovereign = (await client.post("/v1/ai/chat", json=chat_body(QUESTION, **CONFIDENTIAL))).json()
        # With the CIM asleep and bypassed, an external answer has no GPU line, so the GPU budget never
        # refuses it.
        external_payload = chat_body(
            QUESTION, task="qa", evidence=EVIDENCE, routing={"preferred_model": "gemini-flash-lite"}
        )
        external = (await client.post("/v1/ai/chat", json=external_payload)).json()
    assert sovereign["status"] == "blocked" and sovereign["error"]["code"] == "QUOTA_EXCEEDED"
    assert sovereign["error"]["rule_ids"] == [budget_id(INTERNET_GPU, "gct", "monthly")]
    assert await _attempts(db, sovereign["run_id"]) == []  # refused before any billable stage
    assert external["status"] == "completed", external.get("error")
    assert spy.called and "gct-qwen-finetuned" not in spy.called


async def test_without_an_internet_gpu_budget_internet_key_calls_are_refused(
    live_services, make_client, credentials, restore_gpu_cap
) -> None:  # type: ignore[no-untyped-def]
    async with live_services.sessionmaker() as session, session.begin():
        row = await session.get(BudgetRow, budget_id(INTERNET_GPU, "gct", "monthly"))
        assert row is not None
        await session.delete(row)
    await live_services.quota.publish_budgets("gct")
    svc, spy, _ = deployed(live_services, awake=frozenset({"gct-qwen-finetuned"}), measured=True)
    async with make_client(svc=svc, headers=through_proxy(credentials[SANDBOX])) as client:
        body = (await client.post("/v1/ai/chat", json=chat_body(QUESTION, **CONFIDENTIAL))).json()
    assert body["status"] == "blocked" and body["error"]["rule_ids"] == [f"{INTERNET_GPU}:unconfigured"]
    assert spy.called == []
