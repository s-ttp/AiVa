"""Live CIM client: JSON-schema request, structural validation and retry signalling (LLD §9, §17)."""

import json

import httpx2
import pytest

from aiva_gateway.catalog import load_catalog
from aiva_gateway.config import REPO_ROOT
from aiva_gateway.contracts.cim import CimEvidence, CimMessage, CimRequest
from aiva_gateway.core.enums import Provenance
from aiva_gateway.inference.endpoints import ResolvedEndpoint
from aiva_gateway.inference.openai_compatible import OpenAICompatibleAdapter
from aiva_gateway.optimize.cim import EVIDENCE_PREAMBLE, evidence_block
from aiva_gateway.optimize.cim_live import CIM_EXTRA_PARAMS, CIM_SYSTEM_PROMPT, LiveContextIntelligence

CATALOG = load_catalog(REPO_ROOT / "config")
CIM_MODEL = CATALOG.model("enterprise-cim")
REQUEST = CimRequest(
    task="analysis",
    messages=[CimMessage(role="user", content="Correlate")],
    evidence=[
        CimEvidence(
            id="a", text="alarm_id: A1\nsite_id: S1\nraised_at: 2026-10-14T06:00:00Z\nseverity: minor"
        )
    ],
    context_profile="network-alarm-correlation",
    target_budget_tokens=1000,
    required_fields=["alarm_id"],
)
VALID = {
    "intent": "correlate alarms",
    "required_facts": [{"field": "alarm_id", "value": "A1", "source_id": "a"}],
    "selected_evidence_ids": ["a"],
    "omitted_evidence_ids": [],
    "missing_fields": [],
    "upstream_messages": [
        {
            "role": "system",
            "content": f"{EVIDENCE_PREAMBLE}\n{evidence_block('a', REQUEST.evidence[0].text)}",
        },
        {"role": "user", "content": "Correlate"},
    ],
    "self_reported_complete": True,
}


def sse(content: str, finish: str = "stop") -> str:
    chunks = [content[i : i + 40] for i in range(0, len(content), 40)]
    lines = [
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "enterprise-cim-lora",
            "choices": [{"index": 0, "delta": {"content": c}, "finish_reason": None}],
        }
        for c in chunks
    ]
    lines.append(
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "enterprise-cim-lora",
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
        }
    )
    lines.append(
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "enterprise-cim-lora",
            "choices": [],
            "usage": {"prompt_tokens": 300, "completion_tokens": 120, "total_tokens": 420},
        }
    )
    return "".join(f"data: {json.dumps(line)}\n\n" for line in lines) + "data: [DONE]\n\n"


def client(
    content: str | None,
    *,
    finish: str = "stop",
    status: int = 200,
    sent: list | None = None,  # type: ignore[type-arg]
    verified: str | None = None,
) -> LiveContextIntelligence:
    def handler(req: httpx2.Request) -> httpx2.Response:
        if sent is not None:
            sent.append(json.loads(req.content))
        if status != 200:
            return httpx2.Response(status, json={"error": "unavailable"})
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, text=sse(content or "", finish)
        )

    async def endpoint() -> ResolvedEndpoint:
        return ResolvedEndpoint("https://cim.endpoint.example", verified)

    adapter = OpenAICompatibleAdapter(
        api_key="hf-test",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        provenance=Provenance.MEASURED,
        served_model="enterprise-cim-lora",
        base_url=endpoint,
        dedicated_endpoint=True,
        fallback_version=CIM_MODEL.pinned_version if CIM_MODEL else None,
        extra_params=CIM_EXTRA_PARAMS,
    )
    return LiveContextIntelligence(adapter)


async def test_valid_package_and_request_contract() -> None:
    sent: list[dict[str, object]] = []
    assert CIM_MODEL is not None
    result = await client(json.dumps(VALID), sent=sent).package(REQUEST, CIM_MODEL)
    assert result.error_code is None and result.response is not None
    assert result.response.selected_evidence_ids == ["a"]
    assert result.usage.input_tokens == 300 and result.provenance is Provenance.MEASURED
    assert result.revision == "enterprise-cim-lora"  # unverified endpoint: the name the server reports
    body = sent[0]
    assert body["model"] == "enterprise-cim-lora" and body["temperature"] == 0
    assert body["response_format"]["type"] == "json_schema"  # type: ignore[index]
    assert body["response_format"]["json_schema"]["schema"]["title"] == "CimResponse"  # type: ignore[index]
    messages = body["messages"]
    assert messages[0] == {"role": "system", "content": CIM_SYSTEM_PROMPT}  # type: ignore[index]
    assert json.loads(messages[1]["content"]) == REQUEST.model_dump(mode="json")  # type: ignore[index]


async def test_code_fenced_json_is_accepted() -> None:
    assert CIM_MODEL is not None
    result = await client("```json\n" + json.dumps(VALID) + "\n```").package(REQUEST, CIM_MODEL)
    assert result.response is not None


@pytest.mark.parametrize(
    ("content", "finish", "code", "retryable"),
    [
        ("{not json", "stop", "CIM_INVALID_JSON", True),
        (json.dumps({"intent": "x"}), "stop", "CIM_SCHEMA_INVALID", True),
        (json.dumps({**VALID, "unexpected": 1}), "stop", "CIM_SCHEMA_INVALID", True),
        (json.dumps(VALID)[:50], "length", "CIM_TRUNCATED", False),
    ],
)
async def test_invalid_output_is_reported_with_usage(content, finish, code, retryable) -> None:  # type: ignore[no-untyped-def]
    assert CIM_MODEL is not None
    result = await client(content, finish=finish).package(REQUEST, CIM_MODEL)
    assert result.response is None and (result.error_code, result.retryable) == (code, retryable)
    assert result.usage.output_tokens == 120  # the failed call was billed and is metered


async def test_cold_endpoint_is_retryable() -> None:
    assert CIM_MODEL is not None
    result = await client(None, status=503).package(REQUEST, CIM_MODEL)
    assert (result.error_code, result.retryable) == ("ENDPOINT_COLD_START", True)


async def test_revision_names_the_verified_weights() -> None:
    assert CIM_MODEL is not None
    verified = await client(json.dumps(VALID), verified=CIM_MODEL.pinned_version).package(REQUEST, CIM_MODEL)
    assert verified.revision == "s-ttp/aiva-cim-qwen3-4b-v7@16bd341b0286"
    # A call that fails before the server names its model records the pinned weights, not the base revision.
    failed = await client(None, status=503).package(REQUEST, CIM_MODEL)
    assert failed.error_code == "ENDPOINT_COLD_START" and failed.retryable
    assert failed.revision == "s-ttp/aiva-cim-qwen3-4b-v7@16bd341b0286"
