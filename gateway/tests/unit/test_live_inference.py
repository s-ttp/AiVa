"""Live adapter behaviour on mocked transports: error classification, usage accounting, breaker, endpoints."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import httpx2
import pytest

from aiva_gateway.catalog import load_catalog
from aiva_gateway.catalog.models import ModelSpec
from aiva_gateway.config import REPO_ROOT, Settings
from aiva_gateway.core.enums import Provenance
from aiva_gateway.inference.anthropic_adapter import AnthropicAdapter, to_anthropic_messages
from aiva_gateway.inference.base import CircuitBreaker, ProviderCallError, UnavailableAdapter
from aiva_gateway.inference.endpoints import EndpointResolver, ResolvedEndpoint
from aiva_gateway.inference.factory import (
    NO_THINKING,
    build_adapter,
    build_adapter_registry,
    build_resolver,
)
from aiva_gateway.inference.gemini_adapter import GeminiAdapter
from aiva_gateway.inference.openai_compatible import OpenAICompatibleAdapter
from aiva_gateway.inference.recording import REDACTED_ENDPOINT_HOST, RecordingStore
from aiva_gateway.inference.simulated import SimulatedInferenceAdapter
from aiva_gateway.pipeline.interfaces import InferenceRequest

CATALOG = load_catalog(REPO_ROOT / "config")
RECORDINGS = Path(REPO_ROOT, "gateway", "tests", "fixtures", "recordings")
MESSAGES = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Hello synthetic world"}]


def spec(model_id: str) -> ModelSpec:
    model = CATALOG.model(model_id)
    assert model is not None
    return model


def request(model_id: str, timeout: float = 30.0) -> InferenceRequest:
    return InferenceRequest(
        run_id="t", model=spec(model_id), messages=MESSAGES, max_output_tokens=64, timeout_seconds=timeout
    )


def recorded_body(prefix: str) -> str:
    path = sorted(RECORDINGS.glob(f"{prefix}-*.json"))[0]
    return str(json.loads(path.read_text())["response"]["body"])


def anthropic(handler, **kwargs) -> AnthropicAdapter:  # type: ignore[no-untyped-def]
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return AnthropicAdapter(api_key="test", http_client=client, provenance=Provenance.MEASURED, **kwargs)


def openai_compatible(handler, **kwargs) -> OpenAICompatibleAdapter:  # type: ignore[no-untyped-def]
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    kwargs.setdefault("base_url", "https://endpoint.example/v1")
    kwargs.setdefault("served_model", "gct-qwen-lora")
    return OpenAICompatibleAdapter(
        api_key="test", http_client=client, provenance=Provenance.MEASURED, **kwargs
    )


SSE = {"content-type": "text/event-stream"}


async def test_anthropic_success_maps_cache_reads_into_total_input() -> None:
    body = recorded_body("claude-flagship")
    sent: list[dict[str, object]] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(req.content))
        return httpx2.Response(200, headers=SSE, text=body)

    result = await anthropic(handler, effort="low").complete(request("claude-flagship"))
    assert result.status == "ok" and result.provenance is Provenance.MEASURED
    assert result.usage.provenance is Provenance.MEASURED and result.ttft_ms is not None
    assert sent[0]["output_config"] == {"effort": "low"}
    assert sent[0]["system"] == "Be brief." and sent[0]["stream"] is True
    assert "fallbacks" not in sent[0]  # no provider-chosen model switch behind the policy's back


def test_system_messages_become_the_anthropic_system_prompt() -> None:
    system, messages = to_anthropic_messages(
        [
            {"role": "system", "content": "a"},
            {"role": "user", "content": "b"},
            {"role": "tool", "content": "c"},
        ]
    )
    assert system == "a" and messages == [{"role": "user", "content": "b"}, {"role": "user", "content": "c"}]


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (429, "RATE_LIMITED", True),
        (529, "UPSTREAM_ERROR", True),
        (401, "AUTH_FAILED", False),
        (400, "PROVIDER_REJECTED", False),
    ],
)
async def test_anthropic_http_errors_are_classified_and_not_billed(status, code, retryable) -> None:  # type: ignore[no-untyped-def]
    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            status, headers={"retry-after": "0"}, json={"type": "error", "error": {"type": "x"}}
        )

    result = await anthropic(handler).complete(request("claude-flagship"))
    assert (result.status, result.error_code, result.retryable) == ("error", code, retryable)
    assert result.usage.input_tokens == 0 and result.usage.output_tokens == 0


async def test_timeout_charges_an_estimate_labelled_estimated() -> None:
    async def handler(req: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(5)
        return httpx2.Response(200)

    result = await anthropic(handler).complete(request("claude-flagship", timeout=0.05))
    assert result.error_code == "TIMEOUT" and result.retryable
    assert result.usage.input_tokens > 0 and result.usage.provenance is Provenance.ESTIMATED


def _chunk(content: str | None, finish: str | None = None, usage: dict[str, object] | None = None) -> str:
    choices = (
        []
        if usage
        else [{"index": 0, "delta": {"content": content} if content else {}, "finish_reason": finish}]
    )
    payload: dict[str, object] = {
        "id": "c",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": choices,
    }
    if usage:
        payload["usage"] = usage
    return f"data: {json.dumps(payload)}\n\n"


async def test_openai_compatible_without_reported_usage_is_estimated() -> None:
    body = _chunk("Hello") + _chunk(None, "stop") + "data: [DONE]\n\n"
    sent: list[dict[str, object]] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(req.content))
        assert str(req.url) == "https://endpoint.example/v1/chat/completions"
        return httpx2.Response(200, headers=SSE, text=body)

    result = await openai_compatible(handler, dedicated_endpoint=True).complete(request("gct-qwen-finetuned"))
    assert result.status == "ok" and result.content == "Hello" and result.finish_reason == "stop"
    assert result.usage.provenance is Provenance.ESTIMATED  # the server reported no usage
    assert sent[0]["model"] == "gct-qwen-lora" and sent[0]["stream_options"] == {"include_usage": True}


async def test_openai_compatible_reported_usage_is_authoritative() -> None:
    usage = {
        "prompt_tokens": 40,
        "completion_tokens": 12,
        "total_tokens": 52,
        "prompt_tokens_details": {"cached_tokens": 8},
        "completion_tokens_details": {"reasoning_tokens": 5},
    }
    body = _chunk("Hi") + _chunk(None, "stop") + _chunk(None, usage=usage) + "data: [DONE]\n\n"
    result = await openai_compatible(lambda req: httpx2.Response(200, headers=SSE, text=body)).complete(
        request("gct-qwen-finetuned")
    )
    assert (result.usage.input_tokens, result.usage.output_tokens) == (40, 12)
    assert (result.usage.cached_input_tokens, result.usage.reasoning_tokens) == (8, 5)
    assert result.usage.provenance is Provenance.MEASURED


async def test_dedicated_endpoint_503_is_a_retryable_cold_start() -> None:
    result = await openai_compatible(
        lambda req: httpx2.Response(503, json={"error": "scaled to zero"}), dedicated_endpoint=True
    ).complete(request("gct-qwen-finetuned"))
    assert (result.error_code, result.retryable) == ("ENDPOINT_COLD_START", True)


async def test_verified_endpoint_is_labelled_with_the_pinned_weights() -> None:
    model = spec("gct-qwen-finetuned")
    body = _chunk("Hi") + _chunk(None, "stop") + "data: [DONE]\n\n"
    sent: list[dict[str, object]] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(req.content))
        return httpx2.Response(200, headers=SSE, text=body)

    async def verified() -> ResolvedEndpoint:
        return ResolvedEndpoint("https://b.endpoints.example", model.pinned_version)

    adapter = openai_compatible(
        handler, base_url=verified, dedicated_endpoint=True, extra_params=dict(NO_THINKING)
    )
    result = await adapter.complete(request(model.id))
    # The server reports its served name; the record keeps the weights the resolver confirmed.
    assert result.provider_model_version == "s-ttp/aiva-gct-qwen3-32b@f839f6e7810e"
    assert sent[0]["chat_template_kwargs"] == {"enable_thinking": False}


async def test_partial_stream_failure_charges_partial_output_estimate() -> None:
    class Broken(httpx2.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            yield _chunk("partial answer that was already generated").encode()
            raise httpx2.ReadError("connection reset")

    result = await openai_compatible(lambda req: httpx2.Response(200, headers=SSE, stream=Broken())).complete(
        request("deepseek-v4-1-flash")
    )
    assert result.status == "error" and result.retryable
    assert result.usage.output_tokens > 0 and result.usage.provenance is Provenance.ESTIMATED


async def test_gemini_rate_limit_is_retryable() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert "key=" not in str(req.url)  # the API key travels in a header, never the URL
        return httpx.Response(
            429, json={"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}
        )

    adapter = GeminiAdapter(
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        provenance=Provenance.MEASURED,
    )
    result = await adapter.complete(request("gemini-flash-lite"))
    assert (result.error_code, result.retryable) == ("RATE_LIMITED", True)


async def test_gemini_success_bills_thinking_as_output() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=SSE, text=recorded_body("gemini-flash-lite"))

    adapter = GeminiAdapter(
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        provenance=Provenance.REPLAYED,
    )
    result = await adapter.complete(request("gemini-flash-lite"))
    assert result.status == "ok" and result.usage.provenance is Provenance.REPLAYED
    assert result.usage.output_tokens >= result.usage.reasoning_tokens


async def test_a_request_temperature_is_sent_and_none_leaves_the_provider_default() -> None:
    # ADR-0009, 2026-10-22 grounded-answers amendment: the answer temperature travels per request.
    stream = _chunk("Hi") + _chunk(None, "stop") + "data: [DONE]\n\n"
    sent: list[dict[str, object]] = []

    def openai_handler(req: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(req.content))
        return httpx2.Response(200, headers=SSE, text=stream)

    def gemini_handler(req: httpx.Request) -> httpx.Response:
        sent.append(json.loads(req.content))
        return httpx.Response(200, headers=SSE, text=recorded_body("gemini-flash-lite"))

    deepseek = openai_compatible(openai_handler, served_model="deepseek-ai/DeepSeek-V4.1-Flash:deepinfra")
    gemini = GeminiAdapter(
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(gemini_handler)),
        provenance=Provenance.MEASURED,
    )
    for temperature in (None, 0.0):
        await deepseek.complete(replace(request("deepseek-v4-1-flash"), temperature=temperature))
    for temperature in (None, 0.3):
        await gemini.complete(replace(request("gemini-flash-lite"), temperature=temperature))
    assert "temperature" not in sent[0] and sent[1]["temperature"] == 0.0
    configs = [body["generationConfig"] for body in sent[2:]]
    assert configs == [{"maxOutputTokens": 64}, {"temperature": 0.3, "maxOutputTokens": 64}]


def test_breaker_opens_probes_and_recovers() -> None:
    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=0.0)
    breaker.record("RATE_LIMITED")  # a reachable provider never trips the breaker
    breaker.record("TIMEOUT")
    assert breaker.state == "closed"
    breaker.record("UPSTREAM_ERROR")
    assert breaker.state == "half-open"  # reset_seconds=0: immediately eligible for one probe
    assert breaker.allow() and not breaker.allow()  # a single probe at a time
    breaker.record(None)
    assert breaker.state == "closed" and breaker.allow()


async def test_open_breaker_fails_fast_without_calling_the_provider() -> None:
    calls = 0

    def handler(req: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(500, json={"error": {"type": "api_error"}})

    adapter = anthropic(handler, breaker=CircuitBreaker(failure_threshold=1, reset_seconds=60))
    first = await adapter.complete(request("claude-flagship"))
    second = await adapter.complete(request("claude-flagship"))
    assert first.error_code == "UPSTREAM_ERROR" and second.error_code == "CIRCUIT_OPEN"
    assert not second.retryable and calls == 1


async def test_unavailable_adapter_fails_closed() -> None:
    result = await UnavailableAdapter("CREDENTIALS_MISSING").complete(request("claude-flagship"))
    assert (result.status, result.error_code, result.retryable) == ("error", "CREDENTIALS_MISSING", False)


PINNED_B = ("s-ttp/aiva-gct-qwen3-32b", "f839f6e7810ed729a3046080a10689c83bc82689")


def _endpoint_api(
    state: str, url: str | None = "https://b.endpoints.example", weights: tuple[str, str] = PINNED_B
) -> httpx.AsyncClient:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer hf-test"
        assert req.url.path == "/v2/endpoint/acme/gct-qwen-finetuned"
        served = {"repository": weights[0], "revision": weights[1]}
        return httpx.Response(200, json={"status": {"state": state, "url": url}, "model": served})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _resolver(state: str, **kwargs) -> EndpointResolver:  # type: ignore[no-untyped-def]
    return EndpointResolver(
        overrides={}, namespace="acme", token="hf-test", http=_endpoint_api(state, **kwargs)
    )


async def test_endpoint_resolution() -> None:
    model = spec("gct-qwen-finetuned")
    assert model.served_weights == PINNED_B
    override = EndpointResolver(overrides={model.id: "https://vpc.example/"}, namespace=None, token=None)
    # An override cannot be checked, so its calls are not labelled with the pinned version.
    assert await override.resolve(model) == ResolvedEndpoint("https://vpc.example", None)
    running = await _resolver("running").resolve(model)
    assert running == ResolvedEndpoint("https://b.endpoints.example", "s-ttp/aiva-gct-qwen3-32b@f839f6e7810e")
    asleep = await _resolver("scaledToZero").resolve(model)
    assert asleep.url == "https://b.endpoints.example"  # a request wakes it
    for state, code in (("failed", "ENDPOINT_UNAVAILABLE"), ("paused", "ENDPOINT_UNAVAILABLE")):
        with pytest.raises(ProviderCallError) as exc:
            await _resolver(state).resolve(model)
        assert exc.value.code == code and not exc.value.retryable
    with pytest.raises(ProviderCallError) as exc:
        await _resolver("initializing", url=None).resolve(model)
    assert exc.value.code == "ENDPOINT_COLD_START" and exc.value.retryable
    unconfigured = EndpointResolver(overrides={}, namespace=None, token=None)
    with pytest.raises(ProviderCallError) as exc:
        await unconfigured.resolve(model)
    assert exc.value.code == "ENDPOINT_NOT_CONFIGURED"


async def test_endpoint_serving_other_weights_fails_closed() -> None:
    model = spec("gct-qwen-finetuned")
    for weights in (
        (PINNED_B[0], "0" * 40),  # another revision of the fine-tune
        ("Qwen/Qwen3-32B", model.revision),  # the base model, not the fine-tune
    ):
        with pytest.raises(ProviderCallError) as exc:
            await _resolver("running", weights=weights).resolve(model)
        assert exc.value.code == "ENDPOINT_REVISION_MISMATCH" and not exc.value.retryable


def test_recording_store_strips_credentials_and_headers(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path, label="x")
    body = b'{"b": 1, "a": 2}'
    url = "https://api.example/v1/x?key=SECRET&alt=sse"
    assert store.fingerprint("POST", url, body) == store.fingerprint(
        "post", "https://api.example/v1/x?alt=sse", b'{"a":2,"b":1}'
    )
    path = store.save("POST", url, body, 200, "text/event-stream", "data: ok\n\n")
    saved = path.read_text()
    assert "SECRET" not in saved and "authorization" not in saved.lower()
    assert store.load("POST", url, body).response_body == "data: ok\n\n"


def test_recording_store_redacts_private_endpoint_hosts(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path, label="x")
    body = b'{"model": "m"}'
    private = "https://my-private-endpoint.us-east-1.aws.endpoints.huggingface.cloud/v1/chat/completions"
    redacted = f"https://{REDACTED_ENDPOINT_HOST}/v1/chat/completions"
    # Any endpoint host fingerprints as the redacted one, so replay finds the exchange it reads back.
    assert store.fingerprint("POST", private, body) == store.fingerprint("POST", redacted, body)
    saved = store.save("POST", private, body, 200, "application/json", "{}").read_text()
    assert "my-private-endpoint" not in saved and REDACTED_ENDPOINT_HOST in saved
    assert store.load("POST", redacted, body).response_body == "{}"
    # The Endpoints management API and other hosts are left as they are.
    api = "https://api.endpoints.huggingface.cloud/v2/endpoint/ns"
    assert store.fingerprint("GET", api, b"") != store.fingerprint("GET", redacted, b"")


def test_factory_modes_and_fail_closed_wiring() -> None:
    simulated = Settings(inference_mode="simulated")
    resolver = build_resolver(simulated, None)
    assert isinstance(build_adapter(spec("claude-flagship"), simulated, resolver), SimulatedInferenceAdapter)
    live = Settings(inference_mode="live", anthropic_api_key=None, gemini_api_key=None, hf_token=None)
    claude = build_adapter(spec("claude-flagship"), live, build_resolver(live, None))
    assert isinstance(claude, UnavailableAdapter) and claude.error_code == "CREDENTIALS_MISSING"
    kept = Settings(inference_mode="live", simulated_models=["gct-qwen-finetuned"], hf_token="hf-x")
    assert isinstance(
        build_adapter(spec("gct-qwen-finetuned"), kept, build_resolver(kept, None)), SimulatedInferenceAdapter
    )
    # Until ML & Data deploys Endpoints A/B, both stay simulated (and labelled so) by default.
    pending = Settings(inference_mode="live", hf_token="hf-x")
    assert isinstance(build_adapter(spec("gct-qwen-finetuned"), pending, resolver), SimulatedInferenceAdapter)
    deployed = Settings(inference_mode="live", hf_token="hf-x", simulated_models=[])
    sovereign = build_adapter(spec("gct-qwen-finetuned"), deployed, resolver)
    assert isinstance(sovereign, OpenAICompatibleAdapter)
    assert (
        sovereign.dedicated_endpoint and sovereign.served_model == "gct-qwen-lora"
    )  # never the public router
    with pytest.raises(ValueError):
        build_adapter_registry(Settings(inference_mode="turbo"), CATALOG, resolver)
