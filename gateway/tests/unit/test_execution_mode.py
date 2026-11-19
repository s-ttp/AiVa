"""Per-run execution mode (ADR-0019): mode resolution, the per-run replay set and the never-wake check."""

import httpx
import pytest
from pydantic import SecretStr

from aiva_gateway.auth.principal import RunIdentity
from aiva_gateway.catalog import load_catalog
from aiva_gateway.catalog.models import ModelSpec
from aiva_gateway.config import Settings
from aiva_gateway.core.enums import IdentityMode, Provenance, SourceMode
from aiva_gateway.core.errors import ConflictError, PermissionDeniedError
from aiva_gateway.inference.endpoints import EndpointResolver
from aiva_gateway.inference.factory import build_replay_registry, build_replay_resolver
from aiva_gateway.inference.modes import EXECUTION_MODE_UNAVAILABLE, RunMode, resolve_execution_mode
from aiva_gateway.optimize.factory import build_replay_context_intelligence
from aiva_gateway.pipeline.interfaces import InferenceRequest
from tests.live.test_provider_adapters import MESSAGES, NOC_REQUEST, SOVEREIGN_MESSAGES

# A gateway configured live, with live credentials present: the per-run replay set must still never connect.
LIVE = Settings(
    inference_mode="live",
    simulated_models=[],
    endpoint_namespace="s-ttp",
    hf_token=SecretStr("hf_test_not_a_real_token"),
    anthropic_api_key=SecretStr("sk-ant-test-not-a-real-key"),
    gemini_api_key=SecretStr("gemini-test-not-a-real-key"),
)
CATALOG = load_catalog(LIVE.config_dir)


def model(model_id: str) -> ModelSpec:
    spec = CATALOG.model(model_id)
    assert spec is not None
    return spec


# --- Mode resolution ------------------------------------------------------------------------------------


def identity(kind: str = "user", *roles: str) -> RunIdentity:
    return RunIdentity(
        application_id="gct-support-chatbot",
        identity_mode=IdentityMode.AUTHENTICATED,
        source_ip="10.20.1.15",
        source_mode=SourceMode.OBSERVED,
        principal_kind=kind,  # type: ignore[arg-type]
        principal_pseudonym="p-1" if kind == "user" else None,
        roles=roles,
    )


VISITOR = identity("user", "public_visitor")
TESTER = identity("user", "tester")
ANALYST = identity("user", "finance_analyst")  # no chat:live
APPLICATION = identity("application")


def resolve(requested, who: RunIdentity = TESTER, configured: str = "live"):  # type: ignore[no-untyped-def]
    return resolve_execution_mode(requested, configured=configured, identity=who)


def test_visitors_default_to_replay_and_everyone_else_to_the_configured_mode() -> None:
    assert resolve(None, VISITOR) == resolve("replay") == RunMode("replay", per_run_replay=True)
    assert resolve(None) == RunMode("live", per_run_replay=False)
    assert resolve(None, APPLICATION) == RunMode("live", per_run_replay=False)
    assert resolve(None, TESTER, configured="simulated") == RunMode("simulated", per_run_replay=False)


def test_live_is_refused_rather_than_downgraded() -> None:
    for who in (VISITOR, TESTER, APPLICATION):
        with pytest.raises(ConflictError) as refused:
            resolve("live", who, configured="simulated")
        assert refused.value.code == EXECUTION_MODE_UNAVAILABLE
    with pytest.raises(PermissionDeniedError) as denied:
        resolve("live", ANALYST)  # Govern's gate: a user needs chat:live
    assert denied.value.code == "LIVE_NOT_PERMITTED"
    assert resolve("live", VISITOR) == RunMode("live", per_run_replay=False)
    assert resolve("live", APPLICATION) == RunMode("live", per_run_replay=False)  # server-to-server
    assert resolve("live", TESTER, configured="record") == RunMode("record", per_run_replay=False)


# --- The per-run replay set ------------------------------------------------------------------------------


async def complete(model_id: str, messages: list[dict[str, str]]):  # type: ignore[no-untyped-def]
    registry = build_replay_registry(LIVE, CATALOG, build_replay_resolver(LIVE))
    try:
        return await registry.for_model(model(model_id)).complete(
            InferenceRequest(run_id="r", model=model(model_id), messages=messages, max_output_tokens=300)
        )
    finally:
        await registry.aclose()


@pytest.mark.parametrize(
    ("model_id", "messages"),
    [
        ("gemini-flash-lite", MESSAGES),
        ("claude-flagship", MESSAGES),
        ("gct-qwen-finetuned", SOVEREIGN_MESSAGES),
    ],
)
async def test_a_recorded_exchange_is_replayed_without_the_network(
    outbound: list[str], model_id: str, messages: list[dict[str, str]]
) -> None:
    result = await complete(model_id, messages)
    assert result.status == "ok" and result.provenance is Provenance.REPLAYED, result.error_code
    assert outbound == []


@pytest.mark.parametrize("model_id", ["gemini-flash-lite", "claude-flagship", "gct-qwen-finetuned"])
async def test_a_replay_miss_is_simulated_without_the_network(outbound: list[str], model_id: str) -> None:
    free_text = [{"role": "user", "content": "A free-text question nobody recorded."}]
    result = await complete(model_id, free_text)
    assert result.status == "ok" and result.provenance is Provenance.SIMULATED
    assert result.usage.provenance is Provenance.SIMULATED
    assert outbound == []


async def test_an_endpoint_without_recordings_is_a_miss(outbound: list[str], tmp_path) -> None:  # type: ignore[no-untyped-def]
    empty = LIVE.model_copy(update={"recordings_dir": tmp_path})
    registry = build_replay_registry(empty, CATALOG, build_replay_resolver(empty))
    spec = model("gct-qwen-finetuned")
    result = await registry.for_model(spec).complete(
        InferenceRequest(run_id="r", model=spec, messages=SOVEREIGN_MESSAGES, max_output_tokens=300)
    )
    await registry.aclose()
    assert result.provenance is Provenance.SIMULATED and outbound == []


async def test_the_cim_replays_its_package_and_simulates_a_miss(outbound: list[str]) -> None:
    cim = build_replay_context_intelligence(LIVE, CATALOG, build_replay_resolver(LIVE))
    spec = model("enterprise-cim")
    try:
        hit = await cim.package(NOC_REQUEST, spec)
        miss = await cim.package(NOC_REQUEST.model_copy(update={"task": "an unrecorded task"}), spec)
    finally:
        await cim.aclose()  # type: ignore[attr-defined]
    assert hit.response is not None and hit.provenance is Provenance.REPLAYED
    assert miss.response is not None and miss.provenance is Provenance.SIMULATED
    assert outbound == []


# --- Never wake a scaled-to-zero endpoint ----------------------------------------------------------------


def resolver(states: list[str]) -> tuple[EndpointResolver, list[str]]:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        state = states[min(len(calls), len(states)) - 1]
        return httpx.Response(200, json={"status": {"state": state, "url": "https://b.example"}, "model": {}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return EndpointResolver(overrides={}, namespace="s-ttp", token="hf_test", http=http), calls


async def test_is_awake_reads_the_endpoints_api_never_the_endpoint() -> None:
    spec = model("gct-qwen-finetuned")
    cold, calls = resolver(["scaledToZero"])
    assert not await cold.is_awake(spec)
    assert not await cold.is_awake(spec)  # cached: one API read for both checks
    assert calls == ["https://api.endpoints.huggingface.cloud/v2/endpoint/s-ttp/gct-qwen-finetuned"]
    warm, _ = resolver(["running"])
    assert await warm.is_awake(spec)
    stale, calls = resolver(["scaledToZero", "running"])
    assert not await stale.is_awake(spec, ttl_seconds=0.0)
    assert await stale.is_awake(spec, ttl_seconds=0.0) and len(calls) == 2


async def test_is_awake_fails_closed_on_api_errors_and_trusts_overrides() -> None:
    spec = model("gct-qwen-finetuned")
    broken = EndpointResolver(
        overrides={},
        namespace="s-ttp",
        token="hf_test",
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))),
    )
    assert not await broken.is_awake(spec)
    override = EndpointResolver(overrides={spec.id: "https://vpc.example"}, namespace=None, token=None)
    assert await override.is_awake(spec)
