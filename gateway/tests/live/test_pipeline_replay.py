"""`POST /v1/ai/chat` end to end on recorded provider exchanges: policy, reservation, egress checks,
provider usage, TTFT, ledger and spans, all labelled `replayed` (or `measured` while recording).

The prompt is a coding request, a task category the CIM was not trained on, so the CIM is bypassed and the
full context is used (ADR-0018); nothing is simulated in these runs. Endpoint A itself is exercised in
test_endpoint_pipeline.py.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select

from aiva_gateway.config import Settings
from aiva_gateway.db.models import ExecutionRunRow, ModelAttemptRow, TraceSpanRow, UsageLedgerRow
from aiva_gateway.services import Services, build_services
from tests.conftest import chat_body, console
from tests.live.conftest import EXPECTED_PROVENANCE, MODE, RECORDINGS_DIR

PROMPT = (
    "Write a short Python function that returns the synthetic GCT roaming surcharge in AED for a number "
    "of megabytes, at 0.05 AED per MB. Code only."
)


@pytest.fixture(scope="module")
async def replay_services(services: Services, settings: Settings) -> AsyncIterator[Services]:
    replay = settings.model_copy(
        update={
            "inference_mode": MODE,
            "recordings_dir": RECORDINGS_DIR,
            "retrieval_enabled": False,
            "response_cache_enabled": False,
            "simulated_models": [],  # nothing in these runs is simulated
        }
    )
    svc = build_services(replay)
    yield svc
    await svc.aclose()


async def _attempts(db, run_id: str) -> list[ModelAttemptRow]:  # type: ignore[no-untyped-def]
    rows = await db.execute(select(ModelAttemptRow).where(ModelAttemptRow.run_id == run_id))
    return sorted(rows.scalars().all(), key=lambda a: a.sequence)


def _payload(model_id: str, **extra: object) -> dict[str, object]:
    return chat_body(
        PROMPT,
        task="coding",
        routing={"mode": "pinned", "preferred_model": model_id},
        max_output_tokens=400,
        **console("gct-coding-assistant"),
        **extra,
    )


@pytest.mark.parametrize("model_id", ["gemini-flash-lite", "claude-flagship", "deepseek-v4-1-flash"])
async def test_chat_runs_on_provider_usage(model_id, replay_services, tester_client, db) -> None:  # type: ignore[no-untyped-def]
    async with tester_client(svc=replay_services) as client:
        response = await client.post("/v1/ai/chat", json=_payload(model_id, trace=True))
    body = response.json()
    assert response.status_code == 200, (body.get("error"), body.get("status"))
    assert body["status"] == "completed" and body["selected_model"] == model_id
    assert body["response"]["provenance"] == EXPECTED_PROVENANCE.value
    # The run label is the least trustworthy component: the P0 quota service still reserves `simulated`.
    run_label = "simulated" if body["budget"]["provenance"] == "simulated" else EXPECTED_PROVENANCE.value
    assert body["usage"]["provenance"] == run_label
    assert (
        body["cim"]["message"] == "CIM bypassed: task outside the CIM's trained categories; full context used"
    )

    attempts = await _attempts(db, body["run_id"])
    assert [(a.stage, a.status) for a in attempts] == [("upstream", "ok")]  # no CIM call, nothing billed
    upstream = attempts[0]
    assert upstream.provenance == EXPECTED_PROVENANCE.value
    assert upstream.input_tokens > 0 and upstream.output_tokens > 0 and upstream.ttft_ms is not None
    assert upstream.input_tokens == body["usage"]["upstream_submitted_input_tokens"]

    ledger = (
        (await db.execute(select(UsageLedgerRow).where(UsageLedgerRow.run_id == body["run_id"])))
        .scalars()
        .all()
    )
    charged = [row for row in ledger if row.attempt_id == upstream.attempt_id]
    assert len(charged) == 1 and charged[0].measured_or_estimated == EXPECTED_PROVENANCE.value
    expected = replay_services.prices.cost_micros(
        model_id,
        input_tokens=upstream.input_tokens,
        output_tokens=upstream.output_tokens,
        cached_input_tokens=upstream.cached_input_tokens,
    )
    assert charged[0].amount_micros == expected > 0
    assert body["cost"]["total_api_micros"] == expected

    run = await db.get(ExecutionRunRow, body["run_id"])
    assert run is not None and run.provenance == run_label
    span = (
        await db.execute(
            select(TraceSpanRow).where(
                TraceSpanRow.run_id == body["run_id"], TraceSpanRow.name == "inference"
            )
        )
    ).scalar_one()
    assert span.attributes["aiva.inference.provenance"] == EXPECTED_PROVENANCE.value


async def test_baseline_comparison_is_a_separate_policy_checked_charge(
    replay_services, tester_client, db
) -> None:  # type: ignore[no-untyped-def]
    async with tester_client(svc=replay_services) as client:
        body = (
            await client.post("/v1/ai/chat", json=_payload("deepseek-v4-1-flash", baseline_comparison=True))
        ).json()
    assert body["status"] == "completed", body
    attempts = await _attempts(db, body["run_id"])
    assert [(a.stage, a.model_id) for a in attempts] == [
        ("upstream", "deepseek-v4-1-flash"),
        ("baseline", "claude-flagship"),
    ]
    assert attempts[1].status == "ok" and attempts[1].provenance == EXPECTED_PROVENANCE.value
    assert body["cost"]["baseline_micros"] > 0
