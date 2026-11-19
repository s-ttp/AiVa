"""The baseline comparison's own output budget and effort (ADR-0009, 2026-10-18 amendment).

Opus 5.5 thinking cannot be disabled and counts against `max_tokens`, so on the request's budget the frontier
baseline was cut off (S5's spent all 1,024 tokens thinking). The baseline call gets max(the request's budget,
`baseline.max_output_tokens`) and the configured effort; its reservation line is sized with the same number,
so a long baseline settles inside its reservation, and the request cap still applies to it.
"""

from dataclasses import replace
from typing import Any

from sqlalchemy import select

from aiva_gateway.catalog.models import BaselineCall
from aiva_gateway.db.models import BudgetAlertRow, ModelAttemptRow, TraceSpanRow
from aiva_gateway.inference.registry import AdapterRegistry
from aiva_gateway.inference.simulated import SimulatedInferenceAdapter
from aiva_gateway.inference.tokens import input_token_bound
from aiva_gateway.pipeline.interfaces import InferenceRequest, InferenceResult
from aiva_gateway.services import Services
from tests.conftest import chat_body, console, with_overrides

APP = "gct-support-chatbot"
FRONTIER = "claude-flagship"
REQUEST_CAP = "bud-request-default-request"
# S2's baseline at effort low (measured 2026-10-18): 3,512 output tokens, 365 of them thinking.
BASELINE_OUTPUT, BASELINE_THINKING = 3_512, 365


class LongFrontier(SimulatedInferenceAdapter):
    """Bills the input bound; the frontier answers at S2's measured length, capped by what it was allowed."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[InferenceRequest] = []

    async def complete(self, request: InferenceRequest) -> InferenceResult:
        self.requests.append(request)
        result = await super().complete(request)
        if request.model.id != FRONTIER:
            return result
        output = min(BASELINE_OUTPUT, request.max_output_tokens)
        usage = replace(
            result.usage,
            input_tokens=input_token_bound(request.messages),
            output_tokens=output,
            reasoning_tokens=BASELINE_THINKING,
        )
        stop = "end_turn" if output < request.max_output_tokens else "max_tokens"
        return replace(result, usage=usage, finish_reason=stop)


def with_baseline(services: Services, **call: Any) -> Services:
    models = services.catalog.models.model_copy(update={"baseline": BaselineCall(**call)})
    return with_overrides(services, catalog=replace(services.catalog, models=models))


def test_the_frontier_baseline_has_its_own_budget_and_effort(services) -> None:  # type: ignore[no-untyped-def]
    assert services.catalog.models.baseline == BaselineCall(max_output_tokens=6144, effort="low")


async def test_a_long_baseline_answers_in_full_and_settles_inside_its_reservation(
    services, tester_client, db
) -> None:  # type: ignore[no-untyped-def]
    adapter = LongFrontier()
    svc = with_overrides(services, adapters=AdapterRegistry(services.catalog, adapters={}, default=adapter))
    payload = chat_body("What are the Europe roaming rates?", baseline_comparison=True, **console(APP))
    async with tester_client(svc=svc) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed", body.get("error")

    [answer, baseline] = adapter.requests
    assert (answer.max_output_tokens, answer.effort) == (1024, None)  # the request's budget
    assert (baseline.model.id, baseline.max_output_tokens, baseline.effort) == (FRONTIER, 6144, "low")
    attempt = await db.scalar(
        select(ModelAttemptRow).where(
            ModelAttemptRow.run_id == body["run_id"], ModelAttemptRow.stage == "baseline"
        )
    )
    assert attempt is not None and attempt.output_tokens == BASELINE_OUTPUT > 1024

    budget = body["budget"]
    assert 0 < budget["committed_micros"] <= budget["reserved_micros"]
    assert body["cost"]["baseline_micros"] > 0
    alerts = await db.scalars(select(BudgetAlertRow.kind).where(BudgetAlertRow.run_id == body["run_id"]))
    assert "reservation_overrun" not in list(alerts)
    span = await db.scalar(
        select(TraceSpanRow.attributes).where(
            TraceSpanRow.run_id == body["run_id"], TraceSpanRow.name == "inference"
        )
    )
    assert span is not None
    assert span["aiva.baseline.max_output_tokens"] == 6144
    assert span["aiva.baseline.finish_reason"] == "end_turn"


async def test_a_larger_request_budget_wins(services, tester_client) -> None:  # type: ignore[no-untyped-def]
    adapter = LongFrontier()
    svc = with_overrides(services, adapters=AdapterRegistry(services.catalog, adapters={}, default=adapter))
    payload = chat_body(
        "What are the Europe roaming rates?", baseline_comparison=True, max_output_tokens=8000, **console(APP)
    )
    async with tester_client(svc=svc) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed", body.get("error")
    assert [r.max_output_tokens for r in adapter.requests] == [8000, 8000]


async def test_the_request_cap_applies_to_the_baseline_at_its_own_budget(services, tester_client, db) -> None:  # type: ignore[no-untyped-def]
    # 100,000 output tokens at $20/M are $2.00 on their own: only the baseline's own budget, not the
    # request's 1,024 tokens, takes this envelope over the $2 request cap.
    svc = with_baseline(services, max_output_tokens=100_000, effort="low")
    async with tester_client(svc=svc) as client:
        alone = (await client.post("/v1/ai/chat", json=chat_body("Roaming rates?", **console(APP)))).json()
        compared = (
            await client.post(
                "/v1/ai/chat", json=chat_body("Roaming rates?", baseline_comparison=True, **console(APP))
            )
        ).json()
    assert alone["status"] == "completed", alone.get("error")
    assert compared["status"] == "blocked" and compared["outcome"] == "quota_denied"
    assert compared["error"]["code"] == "QUOTA_EXCEEDED" and compared["error"]["rule_ids"] == [REQUEST_CAP]
    attempts = await db.scalars(
        select(ModelAttemptRow.model_id).where(ModelAttemptRow.run_id == compared["run_id"])
    )
    assert list(attempts) == [], "nothing billable ran"
