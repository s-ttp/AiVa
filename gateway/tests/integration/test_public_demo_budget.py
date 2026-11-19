"""Public demo live runs (ADR-0019): the tenant's $10/day `public_demo` budget and each visitor's hourly
live-run limit, refused as LIVE_DEMO_UNAVAILABLE and never downgraded. A visitor here is the run identity a
public session produces (test_public_demo.py covers the sessions themselves): the `public_visitor` role and a
per-session pseudonym."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aiva_gateway.api import chat as chat_api
from aiva_gateway.auth.principal import Caller
from aiva_gateway.catalog.models import ModelSpec
from aiva_gateway.core.enums import Provenance
from aiva_gateway.core.ids import ulid
from aiva_gateway.finops import admin
from aiva_gateway.finops.live import LiveRunWindow, VisitorLimitReached, WindowState
from aiva_gateway.finops.scopes import LIVE, PUBLIC_DEMO, PUBLIC_VISITOR_ROLE, Bucket, period_key, scope_chain
from aiva_gateway.pipeline.interfaces import (
    InferenceAdapter,
    InferenceRequest,
    InferenceResult,
    QuotaDeniedError,
    ReservationRequest,
)
from tests.conftest import chat_body, with_overrides
from tests.integration.test_budgets import APP, set_budget, workflow

DEMO_BUDGET = "bud-public_demo-gct-daily"
QWEN_MS = 1440.0  # $2.50/h (rc-gct-qwen-local): 1,000 micro-USD of estimated GPU time per call


class _Live:
    def __init__(self, inner: InferenceAdapter) -> None:
        self.inner = inner

    async def complete(self, request: InferenceRequest) -> InferenceResult:
        result = await self.inner.complete(request)
        usage = replace(result.usage, provenance=Provenance.MEASURED)
        return replace(result, usage=usage, provenance=Provenance.MEASURED, latency_ms=QWEN_MS)


class LiveAdapters:
    """Stands in for live providers: measured usage and a fixed busy time."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def for_model(self, model: ModelSpec) -> InferenceAdapter:
        return _Live(self.inner.for_model(model))

    async def aclose(self) -> None:
        return None


def live(services: Any, **settings: Any) -> Any:
    updated = services.settings.model_copy(update={"inference_mode": "live", **settings})
    return with_overrides(services, settings=updated, adapters=LiveAdapters(services.adapters))


def sovereign(*, live: bool = False) -> dict[str, Any]:
    # Unique text, so the response cache never answers it, and a sovereign route (GPU time, $0 tokens). A
    # visitor's run is replayed unless it asks for live (ADR-0019).
    body = chat_body(f"Summarise the account notes for case {ulid()}.", data_classification="confidential")
    return {**body, "execution_mode": "live"} if live else body


@pytest.fixture
def visitor(monkeypatch) -> str:
    """Runs through /v1/ai/chat execute as one public visitor (until Govern's public principal lands)."""
    pseudonym = f"usr_{ulid()[-16:].lower()}"
    resolve = chat_api.resolve_run_identity

    def as_visitor(*args: Any, **kwargs: Any) -> Any:
        identity = resolve(*args, **kwargs)
        roles = (*identity.roles, PUBLIC_VISITOR_ROLE)
        return replace(identity, principal_kind="user", principal_pseudonym=pseudonym, roles=roles)

    monkeypatch.setattr(chat_api, "resolve_run_identity", as_visitor)
    return pseudonym


@pytest.fixture
def finance(make_client, user_token):
    return lambda **kw: make_client(user_token("u-finance"), **kw)


@pytest.fixture
async def demo_cap(finance):
    """Set the public-demo daily cap for one test, then restore the configured $10."""

    async def set_cap(cap: int) -> None:
        body = {"scope_type": PUBLIC_DEMO, "scope_id": "gct", "period": "daily", "cap_micros": cap}
        async with finance() as client:
            response = await client.post("/v1/admin/budgets", json=body)
        assert response.status_code == 201, response.text

    yield set_cap
    await set_cap(10_000_000)


async def run(app_client, svc: Any, payload: dict[str, Any]) -> dict[str, Any]:
    async with app_client(APP, svc=svc) as client:
        return (await client.post("/v1/ai/chat", json=payload)).json()


def demo_bucket() -> Bucket:
    return Bucket(PUBLIC_DEMO, "gct", "daily", period_key("daily", datetime.now(UTC)), LIVE)


async def demo_spent(services: Any) -> int:
    state = (await services.quota.bucket_states("gct", [demo_bucket()])).get(demo_bucket().key)
    return state.spent if state else 0


def window(services: Any) -> LiveRunWindow:
    return LiveRunWindow(services.redis, limit=5, window=timedelta(hours=1))


async def test_a_visitor_live_run_counts_api_and_gpu_against_the_public_budget(
    app_client, services, visitor
) -> None:
    before = await demo_spent(services)
    body = await run(app_client, live(services), sovereign(live=True))
    assert body["status"] == "completed", body.get("error")
    assert body["execution_path"] == "sovereign-direct" and body["cost"]["total_api_micros"] == 0
    assert body["cost"]["gpu_estimated_micros"] == 1000
    assert await demo_spent(services) - before == 1000, "a $0-token sovereign run still spends GPU time"
    assert (await window(services).state("gct", visitor)).remaining == 4


async def test_the_visitor_limit_refuses_live_runs_but_not_replay(app_client, services, visitor) -> None:
    capped = live(services, public_live_runs_per_window=1)
    assert (await run(app_client, capped, sovereign(live=True)))["status"] == "completed"
    refused = await run(app_client, capped, sovereign(live=True))
    assert refused["status"] == "blocked" and refused["error"]["code"] == "LIVE_DEMO_UNAVAILABLE"
    details = refused["error"]["details"]
    assert details["reason"] == "visitor_limit" and details["resets_at"] is not None
    assert refused["error"]["retryable"] is True
    assert refused["response"] is None, "never silently downgraded"
    # The same visitor can still run the scenario in replay (simulated here): no live budget involved.
    assert (await run(app_client, services, sovereign()))["status"] == "completed"


async def test_an_exhausted_public_budget_refuses_live_runs_until_midnight(
    app_client, services, visitor, demo_cap
) -> None:
    await demo_cap(1)
    refused = await run(app_client, live(services), sovereign(live=True))
    assert refused["status"] == "blocked" and refused["error"]["code"] == "LIVE_DEMO_UNAVAILABLE"
    assert refused["error"]["details"]["reason"] == "budget"
    assert refused["error"]["rule_ids"] == [DEMO_BUDGET]
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    assert datetime.fromisoformat(refused["error"]["details"]["resets_at"]) == midnight
    # The refused run made no model call, so it does not use up one of the visitor's live runs.
    assert (await window(services).state("gct", visitor)).remaining == 5


async def test_signed_in_users_live_runs_are_not_capped_by_the_public_budget(
    app_client, services, demo_cap
) -> None:
    await demo_cap(1)
    before = await demo_spent(services)
    body = await run(app_client, live(services), sovereign(live=True))
    assert body["status"] == "completed", body.get("error")
    assert await demo_spent(services) == before


async def test_visitors_never_raise_budget_approval_requests(app_client, services, visitor, finance) -> None:
    wf = workflow()
    await set_budget(finance, wf, 1, on_exhaustion="approval_required")
    paid = chat_body(
        f"What are the Europe roaming rates? Case {ulid()}.",
        baseline_comparison=True,
        max_output_tokens=16,
        workflow_id=wf,
    )
    body = await run(app_client, services, paid)
    assert body["status"] == "blocked" and body["error"]["code"] == "QUOTA_EXCEEDED"
    assert body["outcome"] == "quota_denied" and body["approval"]["approval_id"] is None


async def test_public_live_runs_fail_closed_without_a_public_demo_budget(services) -> None:
    tenant = f"no-demo-{ulid()[-8:].lower()}"  # a tenant with no budgets at all
    chain = scope_chain(
        tenant_id=tenant,
        department_id=None,
        application_id=APP,
        principal_pseudonym="usr_visitor00a",
        workflow_id=None,
        public_demo=True,
    )
    request = ReservationRequest(
        run_id=ulid(),
        tenant_id=tenant,
        scope_chain=chain,
        amount_micros=1,
        idempotency_key=ulid(),
        domain=LIVE,
    )
    with pytest.raises(QuotaDeniedError) as denied:
        await services.quota.reserve(request)
    assert denied.value.rule_id == f"{PUBLIC_DEMO}:unconfigured"


async def test_the_live_budget_readout_is_coarse(services, db) -> None:
    caller = Caller(kind="user", tenant_id="gct", pseudonym="usr_visitor00r", roles=frozenset())
    visitor = replace(caller, roles=frozenset({PUBLIC_VISITOR_ROLE}))  # type: ignore[arg-type]
    now = datetime.now(UTC)
    staff = await admin.live_budget(services.quota, db, window(services), caller, now=now)
    assert staff.remaining_percent is not None and staff.remaining_percent % 10 == 0
    assert staff.visitor_runs_remaining is None and staff.provenance in (
        Provenance.MEASURED,
        Provenance.ESTIMATED,
    )
    public = await admin.live_budget(services.quota, db, window(services), visitor, now=now)
    assert public.visitor_runs_remaining == 5 and public.available is (public.remaining_percent != 0)


async def test_the_live_budget_endpoint(make_client, user_token) -> None:
    async with make_client(user_token("u-tester")) as client:
        response = await client.get("/v1/demo/live-budget")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) >= {"available", "remaining_percent", "resets_at", "provenance"}
    assert "spent_micros" not in body and "cap_micros" not in body


async def test_the_live_window_is_atomic_idempotent_and_rolling(services) -> None:
    now = datetime(2026, 10, 15, 12, tzinfo=UTC)
    clock = [now]
    live_window = LiveRunWindow(services.redis, limit=2, window=timedelta(hours=1), clock=lambda: clock[0])
    visitor = f"usr_{ulid()[-16:].lower()}"
    await live_window.take("gct", visitor, "run-a")
    await live_window.take("gct", visitor, "run-a")  # the same run holds one slot
    clock[0] = now + timedelta(minutes=10)
    await live_window.take("gct", visitor, "run-b")
    with pytest.raises(VisitorLimitReached) as full:
        await live_window.take("gct", visitor, "run-c")
    assert full.value.resets_at == now + timedelta(hours=1)  # when run-a leaves the window
    assert await live_window.state("gct", visitor) == WindowState(0, now + timedelta(hours=1))
    await live_window.give_back("gct", visitor, "run-b")
    await live_window.take("gct", visitor, "run-c")
    clock[0] = now + timedelta(hours=1, minutes=1)  # run-a has rolled out
    assert (await live_window.state("gct", visitor)).remaining == 1


async def test_a_limit_of_zero_switches_visitor_live_runs_off(app_client, services, visitor) -> None:
    refused = await run(app_client, live(services, public_live_runs_per_window=0), sovereign(live=True))
    assert refused["status"] == "blocked" and refused["error"]["code"] == "LIVE_DEMO_UNAVAILABLE"
    # Switched off, not exhausted: nothing frees up, so there is no reset time and no point retrying.
    assert refused["error"]["details"] == {"reason": "visitor_limit", "resets_at": None}
    assert refused["error"]["retryable"] is False
    off = LiveRunWindow(services.redis, limit=0, window=timedelta(hours=1))
    with pytest.raises(VisitorLimitReached) as reached:
        await off.take("gct", visitor, "run-x")
    assert reached.value.resets_at is None
    assert await off.state("gct", visitor) == WindowState(0, None)


async def test_a_zero_public_budget_switches_live_runs_off(
    app_client, services, visitor, demo_cap, db
) -> None:
    await demo_cap(0)
    refused = await run(app_client, live(services), sovereign(live=True))
    assert refused["status"] == "blocked" and refused["error"]["code"] == "LIVE_DEMO_UNAVAILABLE"
    assert refused["error"]["details"] == {"reason": "budget", "resets_at": None}
    assert refused["error"]["retryable"] is False
    caller = Caller(kind="user", tenant_id="gct", pseudonym=visitor, roles=frozenset())
    readout = await admin.live_budget(services.quota, db, window(services), caller, now=datetime.now(UTC))
    assert (readout.available, readout.remaining_percent, readout.resets_at) == (False, 0, None)
    assert readout.unavailable_reason == "budget"


async def test_a_visitors_default_run_on_a_live_gateway_is_replayed_and_free(
    app_client, services, visitor
) -> None:
    before = await demo_spent(services)
    body = await run(app_client, live(services), sovereign())  # no execution_mode: replay (ADR-0019)
    assert body["status"] == "completed", body.get("error")
    assert body["budget"]["provenance"] in ("replayed", "simulated"), "a replayed hold is never measured"
    assert await demo_spent(services) == before
    assert (await window(services).state("gct", visitor)).remaining == 5
