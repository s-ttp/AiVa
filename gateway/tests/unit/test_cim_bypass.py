"""CIM bypass (nothing to omit, or too large to build in time) and the real-token output cap (ML & Data,
scenario 5 measurements). The bypass derives from the Context Guard's own context-loss rule."""

import json
from pathlib import Path

import pytest

from aiva_gateway.catalog import load_catalog
from aiva_gateway.config import REPO_ROOT
from aiva_gateway.contracts.cim import CimEvidence, CimMessage, CimRequest, CimResponse
from aiva_gateway.core.canonical import estimate_tokens
from aiva_gateway.core.enums import Provenance
from aiva_gateway.optimize.cim import EVIDENCE_PREAMBLE, evidence_block
from aiva_gateway.optimize.cim_live import (
    CIM_CONTEXT_EXCEEDED,
    CIM_DECODE_TOKENS_PER_S,
    CIM_TIMEOUT_SECONDS,
    NOTHING_TO_OMIT,
    REAL_TOKENS_PER_ESTIMATE,
    TOO_LARGE_TO_BUILD_IN_TIME,
    LiveContextIntelligence,
    cim_bypass_reason,
    cim_max_output_tokens,
    cim_messages,
    package_build_seconds,
)
from aiva_gateway.optimize.context_guard import DeterministicContextGuard, passages_to_keep

CATALOG = load_catalog(REPO_ROOT / "config")
NOC = CATALOG.context_profile("network-alarm-correlation")
FIXTURES = Path(REPO_ROOT, "gateway", "tests", "fixtures")
SCENARIO_5 = json.loads((FIXTURES / "scenarios" / "network-operations.json").read_text())["request"]
ALARMS = [CimEvidence.model_validate(e) for e in SCENARIO_5["evidence"]]
QUESTION = SCENARIO_5["messages"]
# Measured on Endpoint A for scenario 5 at full size: the request and the prompt tokens vLLM reported.
MEASURED = json.loads((FIXTURES / "cim" / "scenario5-60-alarms.json").read_text())
# ML & Data's measurement: the only Guard-passing package for the 60 alarms is 6,260 output tokens.
FULL_PACKAGE_OUTPUT_TOKENS = 6_260


NOTE = CimEvidence(id="note-1", text="Synthetic shift note: the weekly NOC dashboard review moved.")


def test_sixty_alarms_with_unique_facts_have_nothing_to_omit() -> None:
    assert len(ALARMS) == 60
    assert passages_to_keep(ALARMS, QUESTION, NOC) == ALARMS
    assert cim_bypass_reason(ALARMS, QUESTION, NOC) == NOTHING_TO_OMIT


def test_with_retrieved_passages_the_kept_alarms_are_too_large_to_build_in_time() -> None:
    # Scenario 5 with retrieval on: knowledge-base passages carry no alarm facts and could be omitted, but a
    # verifiable package must still reproduce all sixty alarms: about 70 s at 70 tokens/s, over 0.8 x 60 s.
    evidence = [*ALARMS, NOTE]
    assert passages_to_keep(evidence, QUESTION, NOC) == ALARMS
    seconds = package_build_seconds(ALARMS, QUESTION, CIM_DECODE_TOKENS_PER_S)
    assert seconds > 0.8 * CIM_TIMEOUT_SECONDS
    assert cim_bypass_reason(evidence, QUESTION, NOC) == TOO_LARGE_TO_BUILD_IN_TIME
    # A faster endpoint could build it in time: the CIM is called.
    assert cim_bypass_reason(evidence, QUESTION, NOC, decode_tokens_per_s=1_000) is None


def test_small_evidence_with_something_to_omit_calls_the_cim() -> None:
    assert cim_bypass_reason([*ALARMS[:3], NOTE], QUESTION, NOC) is None


def test_a_passage_whose_facts_appear_elsewhere_can_be_omitted() -> None:
    first = ALARMS[0]
    copy = CimEvidence(id="copy-of-first", text=first.text)  # every value is also in the original
    assert first not in passages_to_keep([*ALARMS, copy], QUESTION, NOC)
    # Facts already in the conversation are not lost either.
    said = [*QUESTION, {"role": "user", "content": f"Earlier alarm:\n{first.text}"}]
    assert first not in passages_to_keep(ALARMS, said, NOC)


def test_without_a_profile_or_evidence_the_cim_is_called() -> None:
    assert cim_bypass_reason([*ALARMS[:3], NOTE], QUESTION, None) is None
    assert cim_bypass_reason([], QUESTION, NOC) is None


def test_the_bypass_and_the_guard_agree_on_what_omission_loses() -> None:
    # One shared function: the passage the bypass must keep is the one whose omission the Guard rejects.
    alarm = ALARMS[0]
    request = CimRequest(
        task="analysis",
        messages=[CimMessage.model_validate(m) for m in QUESTION],
        evidence=[alarm, NOTE],
        context_profile=NOC.id,
        target_budget_tokens=NOC.target_budget_tokens,
        required_fields=NOC.required_fields,
    )
    assert passages_to_keep(request.evidence, QUESTION, NOC) == [alarm]

    def package(selected: CimEvidence, omitted: CimEvidence) -> CimResponse:
        system = f"{EVIDENCE_PREAMBLE}\n{evidence_block(selected.id, selected.text)}"
        return CimResponse(
            intent="correlate alarms",
            required_facts=[],
            selected_evidence_ids=[selected.id],
            omitted_evidence_ids=[omitted.id],
            missing_fields=[],
            upstream_messages=[CimMessage(role="system", content=system), *request.messages],
            self_reported_complete=False,
        )

    def context_loss(response: CimResponse) -> list[str]:
        return [
            f for f in DeterministicContextGuard().verify(request, response, NOC).failures if "omitted" in f
        ]

    assert context_loss(package(selected=NOTE, omitted=alarm))  # omitting the kept alarm is rejected
    assert not context_loss(package(selected=alarm, omitted=NOTE))  # omitting the note loses nothing


def test_the_cap_covers_a_full_record_list_package_in_real_tokens() -> None:
    request = CimRequest.model_validate(MEASURED["cim_request"])
    assert len(request.evidence) == 60 and request.target_budget_tokens == 4000
    old_cap = int(4000 * 1.10) + 1024  # estimate units, enforced as real max_tokens: 5,424
    new_cap = cim_max_output_tokens(request, context_window=16_384)
    assert old_cap < FULL_PACKAGE_OUTPUT_TOKENS <= new_cap  # truncated before; fits now
    # The prompt allowance (twice its estimate) covers the prompt tokens vLLM measured for this request.
    prompt = REAL_TOKENS_PER_ESTIMATE * sum(estimate_tokens(m["content"]) for m in cim_messages(request))
    assert prompt >= MEASURED["prompt_tokens"]
    assert new_cap + MEASURED["prompt_tokens"] <= 16_384  # never over the endpoint's context


def test_the_cap_never_exceeds_what_the_context_leaves() -> None:
    request = CimRequest.model_validate(MEASURED["cim_request"])
    assert cim_max_output_tokens(request, context_window=8_192) < cim_max_output_tokens(request, 16_384)
    tight = cim_max_output_tokens(request, context_window=8_192)
    prompt = REAL_TOKENS_PER_ESTIMATE * sum(estimate_tokens(m["content"]) for m in cim_messages(request))
    assert tight == 8_192 - prompt


class NeverCalled:
    provenance = Provenance.REPLAYED

    async def complete(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError("the CIM must not be called when the context leaves no room")


@pytest.mark.parametrize("window", [2_048, 4_096])
async def test_no_room_for_a_package_means_no_call(window: int) -> None:
    model = CATALOG.model("enterprise-cim")
    assert model is not None
    request = CimRequest.model_validate(MEASURED["cim_request"])
    small = model.model_copy(update={"context_window": window})
    result = await LiveContextIntelligence(NeverCalled()).package(request, small)  # type: ignore[arg-type]
    assert result.response is None and result.error_code == CIM_CONTEXT_EXCEEDED and not result.retryable
    assert result.usage.input_tokens == 0 and result.revision == model.pinned_version
    assert result.provenance is Provenance.REPLAYED and result.usage.provenance is Provenance.REPLAYED
