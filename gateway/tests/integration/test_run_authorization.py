"""Run routes authorize before they validate the body (ADR-0007, ADR-0019).

401 without credentials, then 403 for a user who may not run (`authorize_run_caller`, the predicate
`resolve_run_identity` applies), and only then 422. Public visitors and application credentials run as before.
"""

import pytest

from aiva_gateway.services import Services
from tests.conftest import chat_body, console
from tests.integration.test_execution_mode import FREE_TEXT, visitor

DUP = [{"id": "doc-1", "text": "Synthetic A."}, {"id": "doc-1", "text": "Synthetic B."}]
MESSAGES = [{"role": "user", "content": "Summarise the synthetic notes."}]
# Invalid bodies, one per run route: each would be a 422 for a caller allowed to run.
INVALID = {
    "/v1/ai/chat": chat_body("Summarise.", evidence=DUP, **console("gct-knowledge-portal")),
    "/v1/artifacts": {
        "document_type": "docx",
        "title": "Synthetic",
        "instructions": "Draft.",
        "evidence": DUP,
        **console("gct-doc-copilot"),
    },
    "/v1/agents/workflows": {"incident_id": "not-an-incident", **console("gct-noc-assistant")},
    "/v1/policy/evaluate": {"messages": MESSAGES, "evidence": DUP, **console("gct-knowledge-portal")},
}


@pytest.fixture(autouse=True)
async def clear_session_limits(services: Services) -> None:
    async for key in services.redis.scan_iter(match="aiva:public-session:*"):
        await services.redis.delete(key)


@pytest.mark.parametrize("route", INVALID)
async def test_without_credentials_it_is_401(make_client, route: str) -> None:  # type: ignore[no-untyped-def]
    async with make_client() as client:
        response = await client.post(route, json=INVALID[route])
    assert response.status_code == 401, response.text


@pytest.mark.parametrize("user", ["u-finance", "u-auditor"])
@pytest.mark.parametrize("route", INVALID)
async def test_a_user_who_may_not_run_is_refused_before_the_body_is_read(
    make_client, user_token, route: str, user: str
) -> None:  # type: ignore[no-untyped-def]
    async with make_client(user_token(user)) as client:
        response = await client.post(route, json=INVALID[route])
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"


@pytest.mark.parametrize("route", INVALID)
async def test_a_tester_gets_the_validation_error(tester_client, route: str) -> None:  # type: ignore[no-untyped-def]
    async with tester_client() as client:
        response = await client.post(route, json=INVALID[route])
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


async def test_a_user_who_may_not_run_is_still_refused_with_a_valid_body(make_client, user_token) -> None:  # type: ignore[no-untyped-def]
    async with make_client(user_token("u-finance")) as client:
        response = await client.post(
            "/v1/ai/chat", json=chat_body(FREE_TEXT, **console("gct-support-chatbot"))
        )
    assert response.status_code == 403 and response.json()["error"]["code"] == "PERMISSION_DENIED"


async def test_an_application_credential_is_unchanged(app_client) -> None:  # type: ignore[no-untyped-def]
    async with app_client("gct-support-chatbot") as client:
        invalid = await client.post("/v1/ai/chat", json=chat_body("Summarise.", evidence=DUP))
        valid = await client.post("/v1/ai/chat", json=chat_body(FREE_TEXT))
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "VALIDATION_FAILED"
    assert valid.status_code == 200 and valid.json()["status"] == "completed", valid.text


async def test_a_public_visitor_runs_replay_and_capped_live(live_services, make_client) -> None:  # type: ignore[no-untyped-def]
    svc, spy, _asleep, token = await visitor(make_client, live_services)
    # The payload of `test_a_visitor_live_run_bypasses_a_sleeping_cim`: a capped live run to a cloud model.
    live = chat_body(
        FREE_TEXT,
        execution_mode="live",
        evidence=[
            {"id": "kb-1", "text": "Synthetic Traveller 10: 10 GB of data in Europe for 7 days, 99 AED."},
            {"id": "note-1", "text": "Synthetic shift note: the weekly dashboard review moved to Tuesday."},
        ],
        routing={"preferred_model": "gemini-flash-lite"},
        **console("gct-support-chatbot"),
    )
    async with make_client(token, svc=svc) as client:
        replay = await client.post("/v1/ai/chat", json=chat_body(FREE_TEXT, **console("gct-support-chatbot")))
        capped = await client.post("/v1/ai/chat", json=live)
    assert replay.status_code == 200 and replay.json()["execution_mode"] == "replay", replay.text
    assert capped.status_code == 200, capped.text
    assert capped.json()["status"] == "completed" and capped.json()["execution_mode"] == "live"
    assert spy.called  # the live run reached the (simulated) upstream


async def test_repeated_ids_on_the_policy_dry_runs_are_a_validation_error(
    tester_client, make_client, user_token
) -> None:  # type: ignore[no-untyped-def]
    # The dry-runs build a chat request from the caller's evidence: a repeated id is a 422, never a 500.
    async with tester_client() as client:
        evaluate = await client.post("/v1/policy/evaluate", json=INVALID["/v1/policy/evaluate"])
    sample = {"application_id": "gct-knowledge-portal", "messages": MESSAGES, "evidence": DUP}
    async with make_client(user_token("u-platform-admin")) as client:
        simulated = await client.post(
            "/v1/admin/policies/test", json={"cases": [{"name": "dup", "sample": sample}]}
        )
    for response in (evaluate, simulated):
        assert response.status_code == 422, response.text
        assert "evidence ids must be unique; repeated at evidence[1]" in response.text
