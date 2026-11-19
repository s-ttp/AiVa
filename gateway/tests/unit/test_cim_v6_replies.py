"""CIM v6's own replies through the gateway's v2 path, at no cost (ADR-0018).

`fixtures/cim/v6-scenario-replies.jsonl` holds v6's replies to the 8 frozen scenario CIM requests (ML & Data,
`cim-v6-eval`: offline vLLM, greedy, guided with `RESPONSE_SCHEMA_V2`; model
`s-ttp/aiva-cim-qwen3-4b-v6-lora@db799fd`, 2026-10-18). Each row also carries ML's own verdict for the step.
Real v6 output must parse as a v2 selection, fit the v2 output cap, and get the Context Guard verdict that
decides the run: S5's reply reports alarm values its passages do not hold, so the Guard fails it and the run
falls back to the full context. S4's reply passes the Guard although it leaves out GCT-POL-014 §3: relevance
is ML's eval gate, which v6 fails there, not something the Guard can see.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from aiva_gateway.catalog import load_catalog
from aiva_gateway.config import REPO_ROOT, Settings
from aiva_gateway.contracts.cim import CimRequest
from aiva_gateway.optimize.cim import EVIDENCE_PREAMBLE
from aiva_gateway.optimize.cim_live import (
    CIM_CONTEXT_WINDOW,
    cim_bypass_reason,
    cim_max_output_tokens,
    parse_cim_response,
)
from aiva_gateway.optimize.context_guard import DeterministicContextGuard

CATALOG = load_catalog(REPO_ROOT / "config")
REPLIES = Path(REPO_ROOT, "gateway", "tests", "fixtures", "cim", "v6-scenario-replies.jsonl")
ROWS: list[dict[str, Any]] = [json.loads(line) for line in REPLIES.read_text(encoding="utf-8").splitlines()]
V2_RATE = Settings.model_fields["cim_v2_decode_tokens_per_s"].default
GUARD_FAILS = {"S5 alarm correlation": {"value_not_verbatim", "value_not_preserved"}}


def test_the_fixture_is_v6_on_the_eight_frozen_steps() -> None:
    assert len(ROWS) == 8 and len({row["step"] for row in ROWS}) == 8
    assert {row["model"] for row in ROWS} == {
        "s-ttp/aiva-cim-qwen3-4b-v6-lora@db799fdee35466a90652820f47fbe7c0222aec03"
    }
    assert {row["provenance"] for row in ROWS} == {"measured"}
    assert sum(row["expectation_passed"] for row in ROWS) == 6


@pytest.mark.parametrize("row", ROWS, ids=[row["step"] for row in ROWS])
def test_v6_reply_through_the_gateways_v2_path(row: dict[str, Any]) -> None:
    request = CimRequest.model_validate(row["request"])
    profile = CATALOG.context_profile(request.context_profile)
    messages = [m.model_dump() for m in request.messages]
    # The gateway calls the CIM for each of these requests, and v6's reply fits the cap it would send.
    assert (
        cim_bypass_reason(request.evidence, messages, profile, decode_tokens_per_s=V2_RATE, contract="v2")
        is None
    )
    assert row["finish_reason"] == "stop"
    assert row["completion_tokens"] <= cim_max_output_tokens(request, CIM_CONTEXT_WINDOW, "v2")

    response, error = parse_cim_response(row["reply"], request, "v2")
    assert error is None and response is not None
    # The package the gateway builds: selected passages only, in evidence order, then the conversation.
    selected = [e for e in request.evidence if e.id in set(response.selected_evidence_ids)]
    package = response.upstream_messages
    if selected:
        assert package[0].content.startswith(EVIDENCE_PREAMBLE)
        positions = [package[0].content.index(f'id="{e.id}"') for e in selected]
        assert positions == sorted(positions)
        omitted = [e for e in request.evidence if e not in selected]
        assert not any(f'id="{e.id}"' in package[0].content for e in omitted)
    assert package[len(package) - len(request.messages) :] == request.messages

    guard = DeterministicContextGuard().verify(request, response, profile)
    if row["step"] in GUARD_FAILS:
        assert not guard.passed and set(guard.failure_codes) == GUARD_FAILS[row["step"]]
    else:
        assert guard.passed, guard.failures
