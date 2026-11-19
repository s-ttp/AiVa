"""OpenAI-compatible API (ADR-0026): the request mapping, the model rule, refusals, streaming, idempotency and
errors in OpenAI's shape. Inference is simulated; the invariants through this API are in
`tests/invariants/test_openai_compat_invariants.py`, the official SDK in `test_openai_sdk.py`."""

import json
from typing import Any

from sqlalchemy import select

from aiva_gateway.db.models import BudgetReservationRow, ExecutionRunRow, ModelAttemptRow
from aiva_gateway.optimize.rlcd_model import build_rlcd
from tests.conftest import CONSOLE_IP, PEERS, with_overrides
from tests.integration.test_rlcd_pipeline import SpyCache
from tests.scenarios.conftest import capturing
from tests.unit.test_rlcd_model import PARITY, VERSION

PATH = "/v1/chat/completions"
APP = "gct-support-chatbot"
GRANTED = ["claude-flagship", "deepseek-v4-1-flash", "gct-qwen-finetuned", "gemini-flash-lite"]


def ask(content: str = "What does the synthetic Traveller 10 roaming bundle include?", **extra: Any) -> dict:  # type: ignore[type-arg]
    return {"model": "aiva-auto", "messages": [{"role": "user", "content": content}], **extra}


async def test_a_completion_runs_through_the_pipeline_and_names_its_run(app_client, db) -> None:  # type: ignore[no-untyped-def]
    async with app_client(APP) as client:
        response = await client.post(PATH, json=ask(max_completion_tokens=200))
    assert response.status_code == 200, response.text
    body = response.json()
    aiva = body["aiva"]
    assert body["object"] == "chat.completion" and body["id"] == aiva["run_id"]
    assert body["model"] == aiva["selected_model"] in GRANTED
    [choice] = body["choices"]
    assert choice["message"]["role"] == "assistant" and choice["message"]["content"]
    assert choice["finish_reason"] in {"stop", "length"}
    usage = body["usage"]
    assert usage["prompt_tokens"] > 0 and usage["completion_tokens"] > 0
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    assert aiva["policy_action"] == "allow" and aiva["provenance"] == "simulated"
    assert aiva["idempotency_key"].startswith("gen-") and aiva["ignored_parameters"] == []
    run = (
        await db.execute(select(ExecutionRunRow).where(ExecutionRunRow.run_id == aiva["run_id"]))
    ).scalar_one()
    assert run.status == "completed" and run.selected_model == body["model"]


async def test_aiva_auto_and_an_omitted_model_let_rlcd_choose(app_client) -> None:  # type: ignore[no-untyped-def]
    payload = {"messages": [{"role": "user", "content": "Roaming in Oman?"}]}
    async with app_client(APP) as client:
        body = (await client.post(PATH, json=payload)).json()
    assert body["model"] in GRANTED
    assert not any("required by the caller" in r for r in body.get("routing", {}).get("reasons", []))


async def test_a_requested_model_is_served_when_eligible(app_client, db) -> None:  # type: ignore[no-untyped-def]
    async with app_client(APP) as client:
        body = (await client.post(PATH, json=ask(model="deepseek-v4-1-flash"))).json()
    assert body["model"] == body["aiva"]["selected_model"] == "deepseek-v4-1-flash"
    attempts = (
        await db.execute(select(ModelAttemptRow).where(ModelAttemptRow.run_id == body["id"]))
    ).scalars()
    assert {a.model_id for a in attempts if a.stage == "upstream"} == {"deepseek-v4-1-flash"}


async def test_a_requested_model_policy_does_not_allow_is_refused_never_substituted(app_client, db) -> None:  # type: ignore[no-untyped-def]
    # Confidential data takes the sovereign path: an external model is not eligible.
    async with app_client(APP) as client:
        response = await client.post(
            PATH, json=ask(model="claude-flagship"), headers={"X-AiVa-Data-Classification": "confidential"}
        )
    assert response.status_code == 403
    body = response.json()
    assert body["error"] == {
        "message": "Model not allowed by AI policy.",
        "type": "permission_error",
        "code": "model_not_allowed",
    }
    run_id = body["aiva"]["run_id"]
    assert body["aiva"]["status"] == "blocked"
    # Blocked before any billable stage: no reservation and no model call.
    assert (await db.execute(select(ModelAttemptRow).where(ModelAttemptRow.run_id == run_id))).all() == []
    reservations = await db.execute(select(BudgetReservationRow).where(BudgetReservationRow.run_id == run_id))
    assert reservations.all() == []


async def test_a_model_outside_the_applications_grants_is_refused(make_client, user_token) -> None:  # type: ignore[no-untyped-def]
    # The market-intelligence application is granted DeepSeek and Claude only.
    headers = {"X-AiVa-Application": "gct-market-intelligence"}
    async with make_client(user_token("u-tester"), peer=CONSOLE_IP, headers=headers) as client:
        response = await client.post(PATH, json=ask(model="gemini-flash-lite"))
    assert response.status_code == 403 and response.json()["error"]["code"] == "model_not_allowed"


async def test_an_unknown_model_is_not_found_and_starts_no_run(app_client) -> None:  # type: ignore[no-untyped-def]
    for model in ("gpt-4o", "enterprise-cim", "openai-grader"):  # unknown, the CIM, the grader
        async with app_client(APP) as client:
            response = await client.post(PATH, json=ask(model=model))
        body = response.json()
        assert response.status_code == 404 and body["error"]["code"] == "model_not_found", model
        assert body["error"]["param"] == "model" and "aiva" not in body


async def test_parameters_aiva_cannot_honour_are_refused_by_name(app_client) -> None:  # type: ignore[no-untyped-def]
    tool = {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}
    refused: dict[str, dict[str, Any]] = {
        "tools": {"tools": [tool]},
        "n": {"n": 2},
        "logprobs": {"logprobs": True},
        "stop": {"stop": ["END"]},
        "response_format": {"response_format": {"type": "json_object"}},
        "foo": {"foo": 1},  # unknown to the API
    }
    async with app_client(APP) as client:
        for param, extra in refused.items():
            response = await client.post(PATH, json=ask(**extra))
            body = response.json()
            assert response.status_code == 400, param
            assert body["error"]["type"] == "invalid_request_error", param
            assert (body["error"]["code"], body["error"]["param"]) == ("unsupported_parameter", param)
        image = {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}
        content = [{"type": "text", "text": "What is this?"}, image]
        response = await client.post(PATH, json={"messages": [{"role": "user", "content": content}]})
        assert response.status_code == 400 and response.json()["error"]["code"] == "unsupported_content"
        tool_turn = {"role": "tool", "content": "42", "tool_call_id": "c1"}
        response = await client.post(PATH, json={"messages": [{"role": "user", "content": "Hi"}, tool_turn]})
        assert response.status_code == 400 and response.json()["error"]["param"] == "messages.1"


async def test_text_parts_developer_messages_and_ignored_sampling(app_client) -> None:  # type: ignore[no-untyped-def]
    messages = [
        {"role": "developer", "content": "Answer briefly."},
        {
            "role": "user",
            "content": [{"type": "text", "text": "Roaming"}, {"type": "text", "text": "in Oman?"}],
        },
    ]
    payload = {"messages": messages, "temperature": 0.2, "seed": 7, "user": "end-user-42", "n": 1}
    async with app_client(APP) as client:
        body = (await client.post(PATH, json=payload)).json()
    assert body["object"] == "chat.completion"
    assert body["aiva"]["ignored_parameters"] == ["temperature", "seed", "user"]


async def test_governance_fields_come_from_headers_or_metadata(app_client) -> None:  # type: ignore[no-untyped-def]
    async with app_client(APP) as client:
        by_header = await client.post(
            PATH, json=ask(), headers={"X-AiVa-Data-Classification": "confidential"}
        )
        by_metadata = await client.post(PATH, json=ask(metadata={"aiva_data_classification": "confidential"}))
        declared_public = await client.post(PATH, json=ask(metadata={"aiva_data_classification": "public"}))
        bad_tier = await client.post(PATH, json=ask(), headers={"X-AiVa-Quality-Tier": "ultra"})
    for response in (by_header, by_metadata):
        aiva = response.json()["aiva"]
        assert aiva["effective_classification"] == "confidential"
        assert aiva["execution_path"] == "sovereign-direct" and aiva["selected_model"] == "gct-qwen-finetuned"
    # A declared classification can never lower the application's floor (most restrictive wins).
    assert declared_public.json()["aiva"]["effective_classification"] == "internal"
    assert bad_tier.status_code == 400 and bad_tier.json()["error"]["param"] == "X-AiVa-Quality-Tier"


async def test_max_completion_tokens_is_the_runs_output_budget(  # type: ignore[no-untyped-def]
    services, make_client, credentials
) -> None:
    svc, captured = capturing(services)
    async with make_client(credentials[APP], peer=PEERS[APP], svc=svc) as client:
        await client.post(PATH, json=ask(max_completion_tokens=64, max_tokens=999))  # the newer name wins
        await client.post(PATH, json=ask(max_tokens=96))
        await client.post(PATH, json=ask())
    assert [r.max_output_tokens for r in captured.requests] == [64, 96, 1024]


def _events(text: str) -> list[Any]:
    data = [line.removeprefix("data: ") for line in text.splitlines() if line.startswith("data: ")]
    assert data[-1] == "[DONE]"
    return [json.loads(d) for d in data[:-1]]


async def test_streaming_is_buffered_and_sends_the_assured_answer_as_chunks(app_client) -> None:  # type: ignore[no-untyped-def]
    long_question = "Explain the synthetic roaming bundles in detail. " * 3
    async with app_client(APP) as client:
        streamed = await client.post(
            PATH, json=ask(long_question, stream=True, stream_options={"include_usage": True})
        )
    assert streamed.status_code == 200 and streamed.headers["content-type"].startswith("text/event-stream")
    chunks = _events(streamed.text)
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    content = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    finish = [c for c in chunks if c["choices"] and c["choices"][0]["finish_reason"]]
    assert len(finish) == 1 and finish[0]["aiva"]["run_id"] == chunks[0]["id"]
    usage = chunks[-1]
    assert usage["choices"] == [] and usage["usage"]["completion_tokens"] > 0
    assert content  # the whole answer, released after assurance
    assert len({c["id"] for c in chunks}) == 1


async def test_a_blocked_stream_is_an_error_not_a_stream(app_client) -> None:  # type: ignore[no-untyped-def]
    async with app_client(APP) as client:
        response = await client.post(PATH, json=ask("Please use password = hunter2pass", stream=True))
    assert response.status_code == 403 and response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "policy_blocked"


async def test_an_idempotency_key_replays_and_a_generated_one_does_not(app_client) -> None:  # type: ignore[no-untyped-def]
    async with app_client(APP) as client:
        first = await client.post(PATH, json=ask(), headers={"Idempotency-Key": "k-openai-1"})
        again = await client.post(PATH, json=ask(), headers={"Idempotency-Key": "k-openai-1"})
        other = await client.post(
            PATH, json=ask("A different question"), headers={"Idempotency-Key": "k-openai-1"}
        )
        a, b = (await client.post(PATH, json=ask())).json(), (await client.post(PATH, json=ask())).json()
    assert again.headers["Idempotent-Replayed"] == "true" and again.json() == first.json()
    assert first.json()["aiva"]["idempotency_key"] == "k-openai-1"
    assert other.status_code == 409 and other.json()["error"]["code"] == "idempotency_key_reused"
    assert a["id"] != b["id"] and a["aiva"]["idempotency_key"] != b["aiva"]["idempotency_key"]


async def test_user_tokens_name_their_application(make_client, user_token, app_client) -> None:  # type: ignore[no-untyped-def]
    token = user_token("u-tester")
    async with make_client(token, peer=CONSOLE_IP, headers={"X-AiVa-Application": APP}) as client:
        named = await client.post(PATH, json=ask())
    async with make_client(token, peer=CONSOLE_IP) as client:
        unnamed = await client.post(PATH, json=ask())
    async with app_client(APP, headers={"X-AiVa-Application": "gct-coding-assistant"}) as client:
        other_app = await client.post(PATH, json=ask())
    assert named.status_code == 200 and named.json()["object"] == "chat.completion"
    assert unnamed.status_code == 400 and unnamed.json()["error"]["param"] == "X-AiVa-Application"
    assert other_app.status_code == 403 and other_app.json()["error"]["type"] == "permission_error"


async def test_a_user_who_may_not_run_is_refused_before_anything_else(make_client, user_token) -> None:  # type: ignore[no-untyped-def]
    # 401, then 403, then 4xx: an auditor (no `chat:test`) naming no application is refused, not asked to.
    async with make_client(user_token("u-auditor")) as client:
        responses = [
            await client.post(PATH, json=ask()),
            await client.get("/v1/models"),
            await client.get("/v1/models/aiva-auto"),
        ]
    for response in responses:
        assert response.status_code == 403 and response.json()["error"]["type"] == "permission_error"


async def test_errors_take_openais_shape_even_before_the_route(make_client) -> None:  # type: ignore[no-untyped-def]
    async with make_client() as client:
        response = await client.post(PATH, json=ask())
    body = response.json()
    assert response.status_code == 401 and body["error"]["type"] == "authentication_error"
    assert set(body["error"]) <= {"message", "type", "param", "code"}


async def test_models_lists_aiva_auto_and_what_policy_leaves_the_application(app_client) -> None:  # type: ignore[no-untyped-def]
    async with app_client(APP) as client:
        listed = (await client.get("/v1/models")).json()
        one = await client.get("/v1/models/deepseek-v4-1-flash")
        missing = await client.get("/v1/models/enterprise-cim")
    assert listed["object"] == "list"
    assert [m["id"] for m in listed["data"]] == ["aiva-auto", *GRANTED]
    assert all(m["object"] == "model" for m in listed["data"])
    assert one.status_code == 200 and one.json()["owned_by"] == "hf-router"
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "model_not_found"
    # The fraud investigator's floor is confidential: policy leaves it the sovereign model only.
    async with app_client("gct-fraud-investigator") as client:
        sovereign = (await client.get("/v1/models")).json()
    assert [m["id"] for m in sovereign["data"]] == ["aiva-auto", "gct-qwen-finetuned"]


async def test_a_tester_simulates_the_console_source_an_application_cannot(
    make_client, user_token, app_client
) -> None:  # type: ignore[no-untyped-def]
    # Behind the platform proxy the observed source is null, so a tester names the Test Console's network as
    # the console does; an application credential may not simulate anything.
    headers = {"X-AiVa-Application": APP, "X-AiVa-Simulated-Source-IP": CONSOLE_IP}
    async with make_client(user_token("u-tester"), peer="203.0.113.7", headers=headers) as client:
        tester = await client.post(PATH, json=ask())
    async with make_client(
        user_token("u-tester"), peer="203.0.113.7", headers={"X-AiVa-Application": APP}
    ) as client:
        untrusted = await client.post(PATH, json=ask())
    async with app_client(APP, headers={"X-AiVa-Simulated-Source-IP": CONSOLE_IP}) as client:
        application = await client.post(PATH, json=ask())
    assert tester.status_code == 200 and tester.json()["object"] == "chat.completion"
    assert untrusted.status_code == 403 and "AIPEL-NET-001" in untrusted.json()["aiva"]["rule_ids"]
    assert application.status_code == 403 and application.json()["error"]["type"] == "permission_error"


async def test_the_aiva_object_names_the_router_that_chose_the_model(
    services, make_client, credentials
) -> None:  # type: ignore[no-untyped-def]
    # A recorded input (ML's parity row "probe-4"): the RLCD model routes it. The same request again is a
    # cache return, where no routing ran (the knowledge portal shares answers by role). A run refused before
    # routing names no router either.
    svc = with_overrides(services, rlcd=build_rlcd("fixture", token=None, fixture=PARITY), cache=SpyCache())
    question = ask("What does the Traveller roaming bundle include?")
    portal = "gct-knowledge-portal"
    async with make_client(credentials[portal], peer=PEERS[portal], svc=svc) as client:
        routed = (await client.post(PATH, json=question)).json()
        cached = (await client.post(PATH, json=question)).json()
        blocked = (await client.post(PATH, json=ask("Please use password = hunter2pass"))).json()
    assert routed["aiva"]["rlcd_version"] == VERSION and not routed["aiva"]["cache_hit"]
    assert cached["aiva"]["cache_hit"] and cached["aiva"]["rlcd_version"] is None
    assert blocked["error"]["code"] == "policy_blocked" and blocked["aiva"].get("rlcd_version") is None
