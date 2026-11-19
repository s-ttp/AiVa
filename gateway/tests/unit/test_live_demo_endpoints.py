"""Endpoint state, wake and scale-down for the live demo page (ADR-0021), against a mocked HF Endpoints API.

A reading only ever asks the HF Endpoints API. A wake never asks the endpoint for a completion: a paused
endpoint is resumed through the API, a scaled-to-zero one gets one request for its model list. Both actions
are idempotent: an endpoint already in the wanted state gets no request at all.
"""

import httpx
import pytest

from aiva_gateway.catalog import load_catalog
from aiva_gateway.catalog.models import ModelSpec
from aiva_gateway.config import REPO_ROOT
from aiva_gateway.inference.endpoints import ENDPOINTS_API, EndpointResolver

CATALOG = load_catalog(REPO_ROOT / "config")


def spec(model_id: str = "gct-qwen-finetuned") -> ModelSpec:
    found = CATALOG.model(model_id)
    assert found is not None
    return found


def url(name: str = "gct-qwen-finetuned") -> str:
    return f"https://{name}.example"


class FakeHf:
    """The HF Endpoints API and the endpoints behind it, each with its own state; records every request."""

    def __init__(self, state: str, *, endpoint_status: int = 503) -> None:
        self.states = {"gct-qwen-finetuned": state, "enterprise-cim": state}
        self.endpoint_status = endpoint_status
        self.requests: list[tuple[str, str]] = []

    @property
    def state(self) -> str:
        return self.states["gct-qwen-finetuned"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        target = str(request.url)
        self.requests.append((request.method, target))
        assert request.headers["Authorization"] == "Bearer hf_test"
        if target.startswith(ENDPOINTS_API):
            name = target.removeprefix(f"{ENDPOINTS_API}/s-ttp/").split("/")[0]
            if request.method == "GET":
                body = {"status": {"state": self.states[name], "url": url(name)}, "model": {}}
                return httpx.Response(200, json=body)
            if target.endswith("/resume"):
                self.states[name] = "initializing"
            elif target.endswith("/scale-to-zero"):
                self.states[name] = "scaledToZero"
            return httpx.Response(200, json={})
        name = request.url.host.removesuffix(".example")
        assert target == f"{url(name)}/v1/models", target  # the only endpoint request a wake makes
        self.states[name] = "initializing"
        return httpx.Response(self.endpoint_status)

    def resolver(self) -> EndpointResolver:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self))
        return EndpointResolver(overrides={}, namespace="s-ttp", token="hf_test", http=http)


@pytest.mark.parametrize(
    ("hf_state", "state", "detail"),
    [
        ("running", "ready", "running"),
        ("initializing", "waking", "starting (initializing)"),
        ("pending", "waking", "starting (pending)"),
        ("scaledToZero", "asleep", "scaled to zero"),
        ("paused", "asleep", "paused"),
        ("failed", "unavailable", "endpoint state failed"),
    ],
)
async def test_a_reading_maps_the_hf_state_and_never_calls_the_endpoint(hf_state, state, detail) -> None:  # type: ignore[no-untyped-def]
    hf = FakeHf(hf_state)
    reading = await hf.resolver().reading(spec())
    assert (reading.state, reading.detail, reading.hf_state) == (state, detail, hf_state)
    assert hf.requests == [("GET", f"{ENDPOINTS_API}/s-ttp/gct-qwen-finetuned")]


async def test_readings_reuse_a_recent_answer_and_fail_soft() -> None:
    hf = FakeHf("running")
    resolver = hf.resolver()
    await resolver.reading(spec())
    await resolver.reading(spec())
    assert len(hf.requests) == 1  # the page polls; HF is asked at most every few seconds
    broken = EndpointResolver(
        overrides={},
        namespace="s-ttp",
        token="hf_test",
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    reading = await broken.reading(spec())
    assert (reading.state, reading.detail) == ("unavailable", "HF Endpoints API: ENDPOINT_UNAVAILABLE")
    unmanaged = await EndpointResolver(overrides={}, namespace=None, token=None).reading(spec())
    assert (unmanaged.state, unmanaged.detail) == ("unavailable", "endpoint not configured on this gateway")
    override = EndpointResolver(overrides={spec().id: "https://vpc.example"}, namespace=None, token=None)
    assert (await override.reading(spec())).state == "ready"


async def test_a_wake_requests_the_model_list_of_a_scaled_to_zero_endpoint() -> None:
    hf = FakeHf("scaledToZero")
    resolver = hf.resolver()
    woken = await resolver.wake(spec())
    assert (woken.state, woken.detail, woken.action) == ("waking", "wake requested", "wake-request")
    assert hf.requests[-1] == ("GET", f"{url()}/v1/models")
    # The cached state is dropped, so the next reading and any run see HF's own answer.
    assert (await resolver.reading(spec())).state == "waking"
    assert not any(url.endswith("/chat/completions") for _, url in hf.requests)


async def test_a_wake_resumes_a_paused_endpoint_through_the_api() -> None:
    hf = FakeHf("paused")
    woken = await hf.resolver().wake(spec("enterprise-cim"))
    assert (woken.state, woken.action) == ("waking", "resume")
    assert hf.requests[-1] == ("POST", f"{ENDPOINTS_API}/s-ttp/enterprise-cim/resume")


@pytest.mark.parametrize("hf_state", ["running", "initializing", "failed"])
async def test_a_wake_is_idempotent(hf_state) -> None:  # type: ignore[no-untyped-def]
    hf = FakeHf(hf_state)
    woken = await hf.resolver().wake(spec())
    assert woken.action == "none" and woken.hf_state == hf_state
    assert [method for method, _ in hf.requests] == ["GET"]  # read only: nothing to wake


async def test_a_refused_wake_is_reported_unavailable() -> None:
    hf = FakeHf("scaledToZero", endpoint_status=401)
    woken = await hf.resolver().wake(spec())
    assert (woken.state, woken.detail) == ("unavailable", "wake refused (401)")


async def test_scale_to_zero_asks_the_api_and_is_idempotent() -> None:
    hf = FakeHf("running")
    resolver = hf.resolver()
    asleep = await resolver.scale_to_zero(spec())
    assert (asleep.state, asleep.action) == ("asleep", "scale-to-zero")
    assert hf.requests[-1] == ("POST", f"{ENDPOINTS_API}/s-ttp/gct-qwen-finetuned/scale-to-zero")
    again = FakeHf("scaledToZero")
    assert (await again.resolver().scale_to_zero(spec())).action == "none"
    assert [method for method, _ in again.requests] == ["GET"]
