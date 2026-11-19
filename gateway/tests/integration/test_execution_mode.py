"""Per-run execution mode through /v1/ai/chat (ADR-0019).

A public visitor's live run never wakes a scaled-to-zero endpoint: the CIM is bypassed, and a call to a
sleeping Endpoint B is refused. That a replay run never opens a connection is an invariant
(`invariants/test_execution_mode.py`).
"""

import pytest
from sqlalchemy import select

from aiva_gateway.db.models import ModelAttemptRow
from aiva_gateway.finops.scopes import ILLUSTRATIVE, LIVE
from aiva_gateway.inference.registry import AdapterRegistry
from aiva_gateway.inference.simulated import SimulatedInferenceAdapter
from aiva_gateway.pipeline.interfaces import InferenceRequest, InferenceResult
from aiva_gateway.services import Services
from tests.conftest import chat_body, console, with_overrides
from tests.integration.test_budgets import metered, workflow
from tests.integration.test_money_domains import daily, spent, workflow_bucket
from tests.integration.test_public_demo import start_session

APP = "gct-support-chatbot"
FREE_TEXT = "Which synthetic GCT bundle suits a week in Lisbon? Nobody recorded this question."
SOVEREIGN = "Review synthetic fraud case FR-2026-0412 for customer MSISDN +971501234567."


async def _attempts(db, run_id: str) -> list[ModelAttemptRow]:  # type: ignore[no-untyped-def]
    query = select(ModelAttemptRow).where(ModelAttemptRow.run_id == run_id)
    return list((await db.execute(query)).scalars())


async def test_without_a_request_users_run_in_the_configured_mode(services, tester_client) -> None:  # type: ignore[no-untyped-def]
    async with tester_client(svc=services) as client:
        body = (
            await client.post("/v1/ai/chat", json=chat_body(FREE_TEXT, **console("gct-support-chatbot")))
        ).json()
    assert body["status"] == "completed" and body["execution_mode"] == "simulated"


async def test_live_on_a_gateway_that_is_not_live_is_refused_not_downgraded(services, tester_client) -> None:  # type: ignore[no-untyped-def]
    payload = chat_body(FREE_TEXT, execution_mode="live", **console("gct-support-chatbot"))
    async with tester_client(svc=services) as client:
        response = await client.post("/v1/ai/chat", json=payload)
    body = response.json()
    assert response.status_code == 409 and body["error"]["code"] == "EXECUTION_MODE_UNAVAILABLE"
    assert body["status"] == "failed" and body["response"] is None


# --- A public visitor's live run never wakes a sleeping endpoint ----------------------------------------


class Asleep:
    """Endpoints API stand-in: every endpoint is scaled to zero, except those named awake."""

    def __init__(self, awake: frozenset[str] = frozenset()) -> None:
        self.awake = awake
        self.checked: list[str] = []

    async def is_awake(self, model) -> bool:  # type: ignore[no-untyped-def]
        self.checked.append(model.id)
        return model.id in self.awake


class Spy(SimulatedInferenceAdapter):
    """Simulated upstream that records every model it is asked to call."""

    def __init__(self) -> None:
        super().__init__()
        self.called: list[str] = []

    async def complete(self, request: InferenceRequest) -> InferenceResult:
        self.called.append(request.model.id)
        return await super().complete(request)


class NoCim:
    revision = "never-called"

    async def package(self, request, model):  # type: ignore[no-untyped-def]
        raise AssertionError("the CIM must not be called on these runs")


@pytest.fixture(autouse=True)
async def clear_session_limits(services: Services) -> None:
    async for key in services.redis.scan_iter(match="aiva:public-session:*"):
        await services.redis.delete(key)


async def visitor(  # type: ignore[no-untyped-def]
    make_client, live_services: Services, awake: frozenset[str] = frozenset()
) -> tuple[Services, Spy, Asleep, str]:
    """A public visitor's session (Govern, ADR-0019) on a live gateway whose endpoints are asleep."""
    spy, asleep = Spy(), Asleep(awake)
    svc = with_overrides(
        live_services,
        adapters=AdapterRegistry(live_services.catalog, adapters={}, default=spy),
        resolver=asleep,
        cim=NoCim(),
    )
    return svc, spy, asleep, await start_session(make_client, svc=svc)


async def test_a_visitor_defaults_to_replay(live_services, make_client, outbound) -> None:  # type: ignore[no-untyped-def]
    svc, spy, asleep, token = await visitor(make_client, live_services)
    async with make_client(token, svc=svc) as client:
        body = (await client.post("/v1/ai/chat", json=chat_body(FREE_TEXT, **console(APP)))).json()
    assert body["status"] == "completed" and body["execution_mode"] == "replay"
    assert spy.called == [] and asleep.checked == [] and outbound == []


async def test_a_visitor_live_run_refuses_a_sleeping_sovereign_endpoint(
    live_services, make_client, db
) -> None:  # type: ignore[no-untyped-def]
    svc, spy, asleep, token = await visitor(make_client, live_services)
    payload = chat_body(SOVEREIGN, execution_mode="live", **console(APP))
    async with make_client(token, svc=svc) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["execution_path"] == "sovereign-direct"
    assert body["status"] == "blocked" and body["error"]["code"] == "LIVE_DEMO_UNAVAILABLE"
    assert body["error"]["details"] == {"reason": "endpoint_cold", "resets_at": None}
    assert asleep.checked == ["gct-qwen-finetuned"]
    assert spy.called == [] and await _attempts(db, body["run_id"]) == []  # no request to the endpoint


async def test_a_visitor_live_run_bypasses_a_sleeping_cim(live_services, make_client) -> None:  # type: ignore[no-untyped-def]
    svc, spy, asleep, token = await visitor(make_client, live_services)
    evidence = [
        {"id": "kb-1", "text": "Synthetic Traveller 10: 10 GB of data in Europe for 7 days, 99 AED."},
        {"id": "note-1", "text": "Synthetic shift note: the weekly dashboard review moved to Tuesday."},
    ]
    payload = chat_body(
        FREE_TEXT,
        execution_mode="live",
        evidence=evidence,
        routing={"preferred_model": "gemini-flash-lite"},
        **console(APP),
    )
    async with make_client(token, svc=svc) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed", body.get("error")
    assert body["execution_mode"] == "live" and body["execution_path"] == "cim-assisted"
    assert (
        body["cim"]["message"]
        == "CIM bypassed: the CIM endpoint is asleep and is not woken; full context used"
    )
    assert "enterprise-cim" in asleep.checked
    assert spy.called and "gct-qwen-finetuned" not in spy.called


async def test_a_replay_run_on_a_live_gateway_spends_only_in_the_illustrative_domain(
    live_services, app_client, outbound
) -> None:  # type: ignore[no-untyped-def]
    # FinOps' money domains (ADR-0008 amendment) follow the run's mode, not the process's: a replay run on the
    # live Space can never drain a live budget.
    wf = workflow()
    live = [daily("tenant", "gct", LIVE), workflow_bucket(wf, LIVE)]
    before = [await spent(live_services, b) for b in live]
    async with app_client(APP, svc=live_services) as client:
        body = (await client.post("/v1/ai/chat", json=metered(wf, execution_mode="replay"))).json()
    assert body["status"] == "completed", body.get("error")
    assert body["execution_mode"] == "replay" and body["budget"]["committed_micros"] > 0
    assert [await spent(live_services, b) for b in live] == before
    assert await spent(live_services, workflow_bucket(wf, ILLUSTRATIVE)) == body["budget"]["committed_micros"]
    assert outbound == []


async def test_a_visitor_live_run_to_a_sleeping_b_is_refused_before_the_cim_spends(
    live_services, make_client, db
) -> None:  # type: ignore[no-untyped-def]
    # CIM-assisted (the request carries evidence) with Endpoint B as the planned upstream. Endpoint A is
    # awake, so only the up-front check of the planned path stands between the visitor and a CIM call that
    # would be metered before the sleeping B refused the run.
    svc, spy, asleep, token = await visitor(make_client, live_services, awake=frozenset({"enterprise-cim"}))
    payload = chat_body(
        FREE_TEXT,
        execution_mode="live",
        evidence=[
            {"id": "kb-1", "text": "Synthetic Traveller 10: 10 GB of data in Europe for 7 days, 99 AED."}
        ],
        routing={"preferred_model": "gct-qwen-finetuned"},
        **console(APP),
    )
    async with make_client(token, svc=svc) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["execution_path"] == "cim-assisted"
    assert body["status"] == "blocked" and body["outcome"] == "error"
    assert body["error"]["code"] == "LIVE_DEMO_UNAVAILABLE" and not body["error"]["retryable"]
    assert body["error"]["details"] == {"reason": "endpoint_cold", "resets_at": None}
    assert asleep.checked == ["gct-qwen-finetuned"]  # refused before the CIM endpoint is even looked at
    assert not body["cim_invoked"] and spy.called == []  # NoCim would have raised
    assert body["assurance"]["answer_citations"]["status"] == "not_checked"  # halted before upstream
    assert body["budget"]["reserved_micros"] == body["budget"]["committed_micros"] == 0
    assert await _attempts(db, body["run_id"]) == []
