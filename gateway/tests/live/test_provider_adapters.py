"""Live adapters against recorded provider exchanges: request building, stream parsing, usage mapping."""

import httpx

from aiva_gateway.catalog import load_catalog
from aiva_gateway.catalog.models import ModelSpec
from aiva_gateway.contracts.cim import CimEvidence, CimMessage, CimRequest
from aiva_gateway.inference.factory import build_adapter, build_resolver, httpx2_client
from aiva_gateway.inference.openai_compatible import HF_ROUTER_URL, OpenAICompatibleAdapter
from aiva_gateway.optimize.cim_live import CIM_EXTRA_PARAMS, LiveContextIntelligence
from aiva_gateway.optimize.context_guard import DeterministicContextGuard
from aiva_gateway.optimize.factory import build_context_intelligence
from aiva_gateway.pipeline.interfaces import InferenceRequest
from tests.live.conftest import EXPECTED_PROVENANCE, MODE, RECORDING, endpoint_settings, provider_settings

SETTINGS = provider_settings()
CATALOG = load_catalog(SETTINGS.config_dir)
MESSAGES = [
    {"role": "system", "content": "Answer in at most two sentences. All data is synthetic and fictional."},
    {"role": "user", "content": "What does a synthetic GCT Traveller 10 roaming bundle include?"},
]


def model(model_id: str) -> ModelSpec:
    spec = CATALOG.model(model_id)
    assert spec is not None
    return spec


async def complete(model_id: str):  # type: ignore[no-untyped-def]
    spec = model(model_id)
    adapter = build_adapter(spec, SETTINGS, build_resolver(SETTINGS, None))
    try:
        return await adapter.complete(
            InferenceRequest(run_id="rec", model=spec, messages=MESSAGES, max_output_tokens=300)
        )
    finally:
        if (close := getattr(adapter, "aclose", None)) is not None:
            await close()


def assert_measured_shape(result, provenance=EXPECTED_PROVENANCE) -> None:  # type: ignore[no-untyped-def]
    assert result.status == "ok", result.error_code
    assert result.content.strip()
    assert result.provenance is provenance and result.usage.provenance is provenance
    assert result.usage.input_tokens > 0 and result.usage.output_tokens > 0
    assert result.ttft_ms is not None and result.latency_ms >= result.ttft_ms


async def test_gemini_flash_lite() -> None:
    result = await complete("gemini-flash-lite")
    assert_measured_shape(result)
    assert result.provider_model_version == "gemini-2.5-flash-lite"
    assert result.finish_reason == "stop"


async def test_claude_opus_reports_thinking_inside_output_tokens() -> None:
    result = await complete("claude-flagship")
    assert_measured_shape(result)
    assert result.provider_model_version == "claude-opus-5-5"
    assert result.finish_reason == "end_turn"
    # Thinking is billed as output: the reported breakdown never exceeds the billed output.
    assert 0 <= result.usage.reasoning_tokens <= result.usage.output_tokens


async def test_deepseek_via_hf_router_pins_the_provider_route() -> None:
    result = await complete("deepseek-v4-1-flash")
    assert_measured_shape(result)
    assert result.provider_model_version == "deepseek-ai/DeepSeek-V4.1-Flash:deepinfra"


async def test_replay_never_reaches_the_network_for_unrecorded_requests() -> None:
    if MODE != "replay":
        return
    spec = model("gemini-flash-lite")
    adapter = build_adapter(spec, SETTINGS, build_resolver(SETTINGS, None))
    result = await adapter.complete(
        InferenceRequest(
            run_id="rec",
            model=spec,
            messages=[{"role": "user", "content": "A prompt that was never recorded."}],
            max_output_tokens=10,
        )
    )
    assert result.status == "error" and result.error_code == "REPLAY_MISS" and not result.retryable
    assert result.usage.input_tokens == 0


NOC_REQUEST = CimRequest(
    task="analysis",
    messages=[
        CimMessage(
            role="user", content="Correlate the synthetic alarms from cell sites RUH-0412 and RUH-0419."
        )
    ],
    evidence=[
        CimEvidence(
            id="noc-1",
            text="alarm_id: A-7781\nsite_id: RUH-0412\nraised_at: 2026-10-14T06:02:11Z\nseverity: major\n"
            "Link down on microwave hop MW-12.",
        ),
        CimEvidence(
            id="noc-2",
            text="alarm_id: A-7790\nsite_id: RUH-0419\nraised_at: 2026-10-14T06:03:40Z\nseverity: minor\n"
            "High VSWR on sector 2.",
        ),
        CimEvidence(
            id="mkt-1", text="Synthetic marketing note: the Traveller 10 bundle launches in November."
        ),
    ],
    context_profile="network-alarm-correlation",
    target_budget_tokens=1200,
    required_fields=["alarm_id", "site_id", "raised_at", "severity"],
)


async def test_structured_cim_protocol_on_untuned_base_model_is_checked_by_the_guard() -> None:
    """CIM serving protocol (JSON schema output) against the *untuned* base Qwen3-4B-Instruct-2507 served by
    an HF router provider. This is a protocol test, not Endpoint A: the base model returns schema-valid JSON
    and sensible evidence selection but drops fact values from the payload, which the Guard must catch."""
    adapter = OpenAICompatibleAdapter(
        api_key=SETTINGS.hf_token.get_secret_value() if SETTINGS.hf_token and MODE != "replay" else "replay",
        http_client=httpx2_client(SETTINGS, "cim-protocol-base-qwen3-4b"),
        provenance=EXPECTED_PROVENANCE,
        served_model="Qwen/Qwen3-4B-Instruct-2507:featherless-ai",
        base_url=HF_ROUTER_URL,
        extra_params=CIM_EXTRA_PARAMS,
    )
    cim = LiveContextIntelligence(adapter)
    try:
        result = await cim.package(NOC_REQUEST, model("enterprise-cim"))
    finally:
        await cim.aclose()
    assert result.error_code is None and result.response is not None
    assert result.provenance is EXPECTED_PROVENANCE and result.usage.output_tokens > 0
    response = result.response
    assert set(response.selected_evidence_ids) | set(response.omitted_evidence_ids) <= {
        "noc-1",
        "noc-2",
        "mkt-1",
    }
    profile = CATALOG.context_profile("network-alarm-correlation")
    guard = DeterministicContextGuard().verify(NOC_REQUEST, response, profile)
    if MODE == "replay":
        # The recorded base-model package loses fact values; the Guard rejects it, so it is never sent.
        assert not guard.passed
        assert any("not preserved in the upstream payload" in failure for failure in guard.failures)


# --- Endpoints A/B: the merged fine-tunes on HF Inference Endpoints (vLLM) ------------------------------

ENDPOINT_SETTINGS = endpoint_settings()
SOVEREIGN_MESSAGES = [
    {
        "role": "system",
        "content": "You are a GCT fraud analyst assistant. All data is synthetic and fictional.",
    },
    {
        "role": "user",
        "content": "Summarise the timeline of synthetic SIM-swap alert FR-2026-0412 in three bullet points:\n"
        "2026-10-12T08:14:03Z SIM change requested for MSISDN 9715-000-0412 at store ST-17.\n"
        "2026-10-12T08:21:47Z New SIM activated; device IMEI changed.\n"
        "2026-10-12T08:29:10Z Two one-time passwords sent to the number for a banking login.",
    },
]


def expected_version(spec: ModelSpec) -> str:
    # Recording resolves through the HF Endpoints API, which confirms the pinned weights; replay pins the
    # recorded URL, which cannot be checked, so the call keeps the name the server reports.
    return spec.pinned_version if RECORDING else str(spec.adapter)


async def test_endpoint_b_serves_the_pinned_gct_qwen_fine_tune() -> None:
    spec = model("gct-qwen-finetuned")
    async with httpx.AsyncClient() as endpoints_api:
        adapter = build_adapter(spec, ENDPOINT_SETTINGS, build_resolver(ENDPOINT_SETTINGS, endpoints_api))
        try:
            result = await adapter.complete(
                InferenceRequest(run_id="rec", model=spec, messages=SOVEREIGN_MESSAGES, max_output_tokens=300)
            )
        finally:
            await adapter.aclose()  # type: ignore[attr-defined]
    assert_measured_shape(result)
    assert result.provider_model_version == expected_version(spec)
    assert result.finish_reason == "stop"
    # Thinking is off: no reasoning tokens and no think block in the answer.
    assert result.usage.reasoning_tokens == 0 and "<think>" not in result.content
    assert "FR-2026-0412" in result.content or "08:14" in result.content


async def test_endpoint_a_tuned_cim_package_passes_the_guard() -> None:
    spec = model("enterprise-cim")
    async with httpx.AsyncClient() as endpoints_api:
        cim = build_context_intelligence(
            ENDPOINT_SETTINGS, CATALOG, build_resolver(ENDPOINT_SETTINGS, endpoints_api)
        )
        assert isinstance(cim, LiveContextIntelligence)
        try:
            result = await cim.package(NOC_REQUEST, spec)
        finally:
            await cim.aclose()
    assert result.error_code is None and result.response is not None, result.error_code
    assert result.provenance is EXPECTED_PROVENANCE and result.usage.provenance is EXPECTED_PROVENANCE
    assert result.usage.input_tokens > 0 and result.usage.output_tokens > 0
    assert result.revision == expected_version(spec)
    response = result.response
    assert set(response.selected_evidence_ids) >= {"noc-1", "noc-2"}
    assert "mkt-1" in response.omitted_evidence_ids
    guard = DeterministicContextGuard().verify(
        NOC_REQUEST, response, CATALOG.context_profile("network-alarm-correlation")
    )
    assert guard.passed, guard.failures
