"""CIM contract v2 (ADR-0018): the CIM returns selection and facts, the gateway builds the package.

The v2 prompt and schema are what CIM v6 was trained on, pinned here by sha256 (ML & Data hold the same
hashes; `aiva_ml` depends on the gateway, never the reverse). The package the gateway builds is the one a
v1 CIM was trained to copy, and the rest of the pipeline (Context Guard, recall floor, recorder) sees the
same `CimResponse` as under v1.
"""

import hashlib
import json
from typing import Any

import httpx2
import pytest
from pydantic import ValidationError

from aiva_gateway.catalog.models import ModelSpec
from aiva_gateway.config import Settings
from aiva_gateway.contracts.cim import CimEvidence, CimMessage, CimRequest
from aiva_gateway.core.enums import Provenance
from aiva_gateway.inference.endpoints import ResolvedEndpoint
from aiva_gateway.inference.openai_compatible import OpenAICompatibleAdapter
from aiva_gateway.optimize.cim import SimulatedContextIntelligence
from aiva_gateway.optimize.cim_live import (
    CIM_EXTRA_PARAMS,
    CIM_EXTRA_PARAMS_V2,
    CIM_SYSTEM_PROMPT_V2,
    CIM_TIME_BUDGET_SHARE,
    CIM_TIMEOUT_SECONDS,
    RESPONSE_FORMAT_V2,
    RESPONSE_SCHEMA_V2,
    TASK_OUTSIDE_TRAINING,
    TOO_LARGE_TO_BUILD_IN_TIME,
    LiveContextIntelligence,
    build_package,
    cim_bypass_reason,
    cim_max_output_tokens,
    cim_messages,
    constrained_response_format,
    min_cim_output_tokens,
    parse_cim_response,
    selection_build_seconds,
    selection_max_tokens,
)
from aiva_gateway.optimize.context_guard import DeterministicContextGuard
from aiva_gateway.optimize.factory import build_context_intelligence
from tests.unit.test_cim_bypass import ALARMS, NOC, NOTE, QUESTION
from tests.unit.test_cim_live import CATALOG, CIM_MODEL, sse

# Until v6 is measured on Endpoint A: the L4 without speculation (config.py).
V2_RATE = Settings.model_fields["cim_v2_decode_tokens_per_s"].default

REQUEST = CimRequest(
    task="analysis",
    messages=[CimMessage(role="user", content="Which alarm was raised first at S1?")],
    evidence=[
        CimEvidence(id="b", text="alarm_id: B7\nsite_id: S2\nraised_at: 2026-10-14T06:05:00Z"),
        CimEvidence(id="a", text="alarm_id: A1\nsite_id: S1\nraised_at: 2026-10-14T06:00:00Z"),
        CimEvidence(id="n", text="Synthetic shift note: the weekly NOC dashboard review moved."),
    ],
    context_profile="network-alarm-correlation",
    target_budget_tokens=1000,
    required_fields=["alarm_id"],
)
SELECTION = {
    "intent": "first alarm at a site",
    "required_facts": [
        {"field": "alarm_id", "value": "B7", "source_id": "b"},
        {"field": "alarm_id", "value": "A1", "source_id": "a"},
    ],
    # Out of evidence order on purpose: the package keeps evidence order.
    "selected_evidence_ids": ["a", "b"],
    "omitted_evidence_ids": ["n"],
    "missing_fields": [],
    "self_reported_complete": True,
}


def v2_cim(
    content: str, sent: list[dict[str, object]], *, constrain_ids: bool = False
) -> LiveContextIntelligence:
    def handler(req: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(req.content))
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, text=sse(content))

    async def endpoint() -> ResolvedEndpoint:
        return ResolvedEndpoint("https://cim.endpoint.example", None)

    adapter = OpenAICompatibleAdapter(
        api_key="hf-test",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        provenance=Provenance.MEASURED,
        served_model="enterprise-cim-lora",
        base_url=endpoint,
        dedicated_endpoint=True,
        extra_params=CIM_EXTRA_PARAMS_V2,
    )
    return LiveContextIntelligence(adapter, "v2", constrain_ids=constrain_ids)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_prompt_and_schema_are_frozen_as_v6_was_trained() -> None:
    # The text and compact schema CIM v6 is trained on (ML & Data, 2026-10-18). Changing either serves the
    # weights a prompt they never saw: retrain, never edit.
    assert len(CIM_SYSTEM_PROMPT_V2) == 1_045
    assert sha256(CIM_SYSTEM_PROMPT_V2) == "c318e5f144afb683f6baa955ceeae3de14d8b8a69658e666936966483ff1c835"
    compact = json.dumps(RESPONSE_SCHEMA_V2, separators=(",", ":"))
    assert sha256(compact) == "9af8c2b30b5c18505267a55d476fdd69bf7ca1596350f6067f47bcdcc60c45f1"
    assert RESPONSE_FORMAT_V2 == {
        "type": "json_schema",
        "json_schema": {"name": "cim_response", "schema": RESPONSE_SCHEMA_V2, "strict": True},
    }


def test_every_v2_property_is_required_in_schema_order() -> None:
    # Constrained decoding emits keys in schema order, and in v2 the selection is the package.
    assert (
        list(RESPONSE_SCHEMA_V2["properties"])
        == RESPONSE_SCHEMA_V2["required"]
        == [
            "intent",
            "required_facts",
            "selected_evidence_ids",
            "omitted_evidence_ids",
            "missing_fields",
            "self_reported_complete",
        ]
    )


async def test_the_package_is_the_one_a_v1_cim_was_trained_to_copy() -> None:
    # The simulated CIM keeps every passage and writes the v1 package; the gateway builds the same bytes.
    # ML & Data score v6 on this package (`aiva_ml.cim.prompt.build_package`): selected ids as a set, in
    # evidence order, unknown ids adding nothing.
    assert CIM_MODEL is not None
    simulated = await SimulatedContextIntelligence().package(REQUEST, CIM_MODEL)
    assert simulated.response is not None
    every_id = [e.id for e in REQUEST.evidence]
    assert build_package(REQUEST, every_id) == simulated.response.upstream_messages
    # Nothing selected: the conversation alone, with no empty evidence message.
    assert build_package(REQUEST, []) == REQUEST.messages


async def test_a_v2_reply_becomes_the_pipelines_cim_response() -> None:
    assert CIM_MODEL is not None
    sent: list[dict[str, object]] = []
    result = await v2_cim(json.dumps(SELECTION), sent).package(REQUEST, CIM_MODEL)
    assert result.error_code is None and result.response is not None
    body = sent[0]
    assert body["messages"] == cim_messages(REQUEST, "v2")
    assert body["messages"][0] == {"role": "system", "content": CIM_SYSTEM_PROMPT_V2}  # type: ignore[index]
    assert body["response_format"]["json_schema"]["schema"] == RESPONSE_SCHEMA_V2  # type: ignore[index]
    response = result.response
    assert response.selected_evidence_ids == ["a", "b"] and response.omitted_evidence_ids == ["n"]
    system = response.upstream_messages[0].content
    assert system.index('id="b"') < system.index('id="a"')  # evidence order, not selection order
    assert 'id="n"' not in system and response.upstream_messages[1:] == REQUEST.messages
    # The Context Guard verifies the built package exactly as a v1 package.
    assert NOC is not None
    guard = DeterministicContextGuard().verify(REQUEST, response, NOC)
    assert guard.passed, guard.failures


@pytest.mark.parametrize(
    "reply",
    [
        # A package of its own: the v2 schema has no place for it.
        {**SELECTION, "upstream_messages": [{"role": "user", "content": "x"}]},
        # Every v2 property is required, the selection above all.
        {k: v for k, v in SELECTION.items() if k != "selected_evidence_ids"},
        {k: v for k, v in SELECTION.items() if k != "required_facts"},
    ],
)
def test_a_reply_outside_the_v2_schema_is_rejected(reply: dict[str, object]) -> None:
    assert parse_cim_response(json.dumps(reply), REQUEST, "v2") == (None, "CIM_SCHEMA_INVALID")


def test_v1_parsing_is_unchanged() -> None:
    # A v2 reply is not a v1 package: v1 still requires `upstream_messages`.
    assert parse_cim_response(json.dumps(SELECTION)) == (None, "CIM_SCHEMA_INVALID")


def test_the_v2_cap_bounds_the_selection_not_a_package() -> None:
    # 128 + the ids ('"b"' etc.: 3 bytes + 1 each) + 40 x 3 passages x 1 required field.
    assert selection_max_tokens(REQUEST) == 128 + 3 * 4 + 40 * 3 * 1
    assert cim_max_output_tokens(REQUEST, contract="v2") == selection_max_tokens(REQUEST)
    assert cim_max_output_tokens(REQUEST, contract="v2") < cim_max_output_tokens(
        REQUEST
    )  # v1 copies passages
    # Sixty alarms x four required fields fit Endpoint A's context; a smaller window binds instead.
    assert NOC is not None
    alarms = REQUEST.model_copy(update={"evidence": ALARMS, "required_fields": NOC.required_fields})
    assert len(NOC.required_fields) == 4 and selection_max_tokens(alarms) > 40 * 60 * 4
    assert cim_max_output_tokens(alarms, 16_384, "v2") == selection_max_tokens(alarms)
    smaller, smallest = cim_max_output_tokens(alarms, 9_000, "v2"), cim_max_output_tokens(alarms, 8_000, "v2")
    assert smallest < smaller < selection_max_tokens(alarms) and smaller - smallest == 1_000


def test_a_v2_selection_too_large_to_emit_in_time_is_bypassed() -> None:
    # Scenario 5 with retrieval on: a v1 CIM would have to emit all sixty alarms, and a v2 CIM still emits
    # every required fact, 60 alarms x 4 fields = 240 entries: about 9.8k tokens, over 5 minutes at 29 tok/s.
    assert NOC is not None and len(NOC.required_fields) == 4
    evidence = [*ALARMS, NOTE]
    budget = CIM_TIME_BUDGET_SHARE * CIM_TIMEOUT_SECONDS
    assert selection_build_seconds(evidence, NOC, V2_RATE) > 5 * budget
    assert cim_bypass_reason(evidence, QUESTION, NOC) == TOO_LARGE_TO_BUILD_IN_TIME
    v2 = cim_bypass_reason(evidence, QUESTION, NOC, decode_tokens_per_s=V2_RATE, contract="v2")
    assert v2 == TOO_LARGE_TO_BUILD_IN_TIME
    # Six alarms (24 facts, about 1.2k tokens): emitted in time, so the CIM is called under either contract.
    small = [*ALARMS[:6], NOTE]
    assert selection_build_seconds(small, NOC, V2_RATE) < budget
    assert cim_bypass_reason(small, QUESTION, NOC, decode_tokens_per_s=V2_RATE, contract="v2") is None
    assert cim_bypass_reason(small, QUESTION, NOC) is None


def test_the_cim_models_contract_selects_prompt_and_schema() -> None:
    assert CIM_MODEL is not None and CIM_MODEL.cim_contract == "v2"  # v7 (ADR-0018)
    settings = Settings(inference_mode="live", simulated_models=[], hf_token="hf-test")  # type: ignore[call-arg]
    for contract, params in (("v1", CIM_EXTRA_PARAMS), ("v2", CIM_EXTRA_PARAMS_V2)):
        model = CIM_MODEL.model_copy(update={"cim_contract": contract})
        models = CATALOG.models.model_copy(
            update={"models": [model if m.id == model.id else m for m in CATALOG.models.models]}
        )
        catalog = type(CATALOG)(**{**CATALOG.__dict__, "models": models})
        cim = build_context_intelligence(settings, catalog, resolver=None)  # type: ignore[arg-type]
        assert isinstance(cim, LiveContextIntelligence) and cim.contract == contract
        assert cim._adapter.extra_params == params  # type: ignore[attr-defined]
        # The runaway guard is on by default (ML & Data's L4 bench) and applies to a v2 CIM only.
        assert cim.constrain_ids is (contract == "v2")
        unguarded = build_context_intelligence(
            settings.model_copy(update={"cim_v2_constrain_ids": False}),
            catalog,
            resolver=None,  # type: ignore[arg-type]
        )
        assert isinstance(unguarded, LiveContextIntelligence) and not unguarded.constrain_ids


def test_only_the_cim_takes_a_contract() -> None:
    upstream = CATALOG.model("gemini-flash-lite")
    assert upstream is not None
    with pytest.raises(ValidationError, match="apply to the CIM only"):
        ModelSpec.model_validate({**upstream.model_dump(), "cim_contract": "v2"})


# v6 and v7 are trained on these task categories, not on coding or generation (ML & Data, 2026-10-18).
TRAINED = ["qa", "rag", "agentic", "summarization", "analysis"]


def test_a_task_outside_the_cims_training_bypasses_it() -> None:
    small = [*ALARMS[:3], NOTE]
    bypass = cim_bypass_reason(small, QUESTION, NOC, contract="v2", task="coding", trained_tasks=TRAINED)
    assert bypass == TASK_OUTSIDE_TRAINING
    assert (
        cim_bypass_reason(small, QUESTION, NOC, contract="v2", task="analysis", trained_tasks=TRAINED) is None
    )
    # Out of distribution with or without evidence.
    assert cim_bypass_reason([], QUESTION, NOC, task="coding", trained_tasks=TRAINED) == TASK_OUTSIDE_TRAINING
    # No list: no restriction, whatever the task.
    assert cim_bypass_reason(small, QUESTION, NOC, contract="v2", task="coding") is None


def test_only_the_cim_takes_trained_tasks() -> None:
    upstream = CATALOG.model("gemini-flash-lite")
    assert upstream is not None and CIM_MODEL is not None and CIM_MODEL.cim_trained_tasks == TRAINED
    with pytest.raises(ValidationError, match="apply to the CIM only"):
        ModelSpec.model_validate({**upstream.model_dump(), "cim_trained_tasks": ["qa"]})


def strip_guard(schema: dict[str, Any]) -> dict[str, Any]:
    properties = dict(schema["properties"])
    for key in ("selected_evidence_ids", "omitted_evidence_ids"):
        prop = {k: v for k, v in properties[key].items() if k != "maxItems"}
        properties[key] = {**prop, "items": {k: v for k, v in prop["items"].items() if k != "enum"}}
    return {**schema, "properties": properties}


def test_the_runaway_guard_only_narrows_the_pinned_schema() -> None:
    # ML & Data: 4 of 2,000 v7 replies looped to the token limit, inventing ids past the end of 43-52-row
    # timeline lists. The guard confines both id lists to the ids sent, one entry per passage at most.
    pinned = json.dumps(RESPONSE_SCHEMA_V2, separators=(",", ":"))
    guarded = constrained_response_format(REQUEST)
    schema = guarded["json_schema"]["schema"]
    for key in ("selected_evidence_ids", "omitted_evidence_ids"):
        assert schema["properties"][key]["items"] == {"type": "string", "enum": ["b", "a", "n"]}
        assert schema["properties"][key]["maxItems"] == 3
    # Only `enum` and `maxItems` are added (both can only reject); everything else is the pinned schema.
    assert strip_guard(schema) == RESPONSE_SCHEMA_V2
    assert {
        **guarded,
        "json_schema": {**guarded["json_schema"], "schema": RESPONSE_SCHEMA_V2},
    } == RESPONSE_FORMAT_V2
    assert json.dumps(RESPONSE_SCHEMA_V2, separators=(",", ":")) == pinned  # a copy: the pin is untouched
    assert constrained_response_format(REQUEST.model_copy(update={"evidence": []})) == RESPONSE_FORMAT_V2


@pytest.mark.parametrize("constrain_ids", [False, True])
async def test_a_guarded_v2_call_sends_the_narrowed_schema(constrain_ids: bool) -> None:
    assert CIM_MODEL is not None
    sent: list[dict[str, object]] = []
    cim = v2_cim(json.dumps(SELECTION), sent, constrain_ids=constrain_ids)
    result = await cim.package(REQUEST, CIM_MODEL)
    assert result.error_code is None and result.response is not None
    expected = constrained_response_format(REQUEST) if constrain_ids else RESPONSE_FORMAT_V2
    assert sent[0]["response_format"] == expected
    assert sent[0]["temperature"] == 0  # the other extra params are unchanged


def test_only_a_v2_cim_is_guarded() -> None:
    adapter = v2_cim("{}", [])._adapter
    assert not LiveContextIntelligence(adapter, "v1", constrain_ids=True).constrain_ids


async def test_a_small_v2_request_is_not_refused_for_asking_less_than_a_package() -> None:
    # One passage, one required field: the v2 selection bound (about 180 tokens) is under the v1 package
    # minimum (256), and the endpoint's context has room for all of it, so the CIM is called.
    assert CIM_MODEL is not None
    small = REQUEST.model_copy(update={"evidence": REQUEST.evidence[1:2]})
    assert selection_max_tokens(small) < 256 == min_cim_output_tokens(small, "v1")
    assert min_cim_output_tokens(small, "v2") == selection_max_tokens(small)
    reply = {**SELECTION, "required_facts": SELECTION["required_facts"][1:], "selected_evidence_ids": ["a"]}
    sent: list[dict[str, object]] = []
    result = await v2_cim(json.dumps({**reply, "omitted_evidence_ids": []}), sent).package(small, CIM_MODEL)
    assert result.error_code is None and result.response is not None
    assert sent[0]["max_tokens"] == selection_max_tokens(small)
