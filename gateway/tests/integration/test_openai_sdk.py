"""The official `openai` Python SDK against the gateway (ADR-0026), in-process and simulated: a completion, a
streamed one, the model list and errors, exactly as an application calls AiVa on Space 2 with a
`gct-api-sandbox` key, whose application takes key calls from the internet (ADR-0027 §3):

    client = openai.OpenAI(base_url="https://<gateway>/v1", api_key="aiva_…")
    client.chat.completions.create(model="aiva-auto", messages=[{"role": "user", "content": "…"}])
"""

import httpx2
import openai
import pytest
from openai.types.chat import ChatCompletionMessageParam
from sqlalchemy import select

from aiva_gateway.db.models import PolicyEventRow
from aiva_gateway.main import create_app
from tests.integration.test_internet_gpu import deployed
from tests.integration.test_public_demo import proof, proxied

APP = "gct-api-sandbox"


def sdk(services, key: str, max_retries: int = 0) -> openai.AsyncOpenAI:  # type: ignore[no-untyped-def]
    """The SDK behind Space 2's front door (deploy/spaces/shared/gateway-proxy.sh): the key the SDK sends as
    `Authorization` arrives as X-AiVa-Authorization with the proxy's proof, and the policy source is null."""
    svc = proxied(services)
    app = create_app(settings=svc.settings, services=svc)
    app.state.services = svc

    async def front_door(request: httpx2.Request) -> None:
        request.headers["X-AiVa-Authorization"] = request.headers.pop("Authorization")
        request.headers.update(proof())

    transport = httpx2.ASGITransport(app=app, client=("127.0.0.1", 50000))
    return openai.AsyncOpenAI(
        base_url="http://gateway/v1",
        api_key=key,
        http_client=httpx2.AsyncClient(transport=transport, event_hooks={"request": [front_door]}),
        max_retries=max_retries,
    )


async def test_the_openai_sdk_completes_streams_and_lists_models(services, credentials, db) -> None:  # type: ignore[no-untyped-def]
    client = sdk(services, credentials[APP])
    question: list[ChatCompletionMessageParam] = [
        {"role": "user", "content": "What does the synthetic Traveller 10 bundle include?"}
    ]
    completion = await client.chat.completions.create(
        model="aiva-auto",
        messages=question,
        max_completion_tokens=200,
    )
    aiva = (completion.model_extra or {})["aiva"]
    assert completion.id == aiva["run_id"] and completion.model == aiva["selected_model"]
    assert completion.choices[0].message.content and completion.usage and completion.usage.total_tokens > 0
    # Behind the front door the source is null: the sandbox's key is admitted as an internet caller.
    networks = await db.execute(
        select(PolicyEventRow.matched_network_id).where(PolicyEventRow.run_id == completion.id)
    )
    assert set(networks.scalars()) == {"internet"}

    stream = await client.chat.completions.create(
        model="aiva-auto",
        messages=question,
        stream=True,
        stream_options={"include_usage": True},
    )
    pieces, usage = [], None
    async for chunk in stream:
        if chunk.choices:
            pieces.append(chunk.choices[0].delta.content or "")
        usage = chunk.usage or usage
    assert "".join(pieces) and usage is not None and usage.completion_tokens > 0

    models = [m.id async for m in client.models.list()]
    assert models[0] == "aiva-auto" and "claude-flagship" in models


async def test_the_openai_sdk_raises_its_own_errors_for_aiva_refusals(services, credentials) -> None:  # type: ignore[no-untyped-def]
    client = sdk(services, credentials[APP])
    with pytest.raises(openai.PermissionDeniedError) as blocked:
        await client.chat.completions.create(
            model="aiva-auto", messages=[{"role": "user", "content": "Please use password = hunter2pass"}]
        )
    assert blocked.value.code == "policy_blocked" and "Blocked by AI policy" in blocked.value.message
    with pytest.raises(openai.BadRequestError) as tools:
        await client.chat.completions.create(
            model="aiva-auto",
            messages=[{"role": "user", "content": "Look it up"}],
            tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
        )
    assert tools.value.code == "unsupported_parameter"
    with pytest.raises(openai.NotFoundError):
        await client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "Hi"}])


async def test_another_applications_key_is_blocked_from_the_internet(services, credentials) -> None:  # type: ignore[no-untyped-def]
    # Only the owner's three applications take key calls from the internet (ADR-0027 §3).
    client = sdk(services, credentials["gct-support-chatbot"])
    with pytest.raises(openai.PermissionDeniedError) as blocked:
        await client.chat.completions.create(model="aiva-auto", messages=[{"role": "user", "content": "Hi"}])
    assert blocked.value.code == "policy_blocked"
    assert "AIPEL-NET-001" in blocked.value.response.json()["aiva"]["rule_ids"]


async def test_a_sleeping_endpoint_is_a_503_the_sdk_does_not_retry(live_services, credentials) -> None:  # type: ignore[no-untyped-def]
    # An internet key call never wakes a scaled-to-zero endpoint (ADR-0027 §3). Retrying at once cannot
    # change that, so the response tells the SDK not to (`x-should-retry: false`): one refused run, not three.
    svc, _, asleep = deployed(live_services)
    client = sdk(svc, credentials[APP], max_retries=2)
    with pytest.raises(openai.InternalServerError) as cold:
        await client.chat.completions.create(
            model="aiva-auto",
            messages=[{"role": "user", "content": "Summarise the synthetic ticket"}],
            extra_headers={"X-AiVa-Data-Classification": "confidential", "X-AiVa-Task": "summarization"},
        )
    # 503, never 502: Space 2's front door replaces a 502 with its own "starting" page (gateway-proxy.sh).
    assert cold.value.status_code == 503 and cold.value.code == "live_demo_unavailable"
    aiva = cold.value.response.json()["aiva"]
    assert aiva["details"]["reason"] == "endpoint_cold" and aiva["retryable"] is False and aiva["remediation"]
    assert cold.value.response.headers["x-should-retry"] == "false"
    assert asleep.checked == ["gct-qwen-finetuned"]
