"""Workflow budget readout for agent workflows (`finops.workflows.workflow_budget`, LLD §12, §15 #6)."""

from typing import Any

import pytest

from aiva_gateway.core.enums import Provenance
from aiva_gateway.finops.scopes import ILLUSTRATIVE
from aiva_gateway.finops.workflows import WorkflowBudget, workflow_budget
from tests.conftest import chat_body
from tests.integration.test_budgets import APP, metered, set_budget, workflow

TEMPLATE = "bud-workflow-default-workflow"


@pytest.fixture
def finance(make_client, user_token):
    return lambda **kw: make_client(user_token("u-finance"), **kw)


async def readout(services, wf: str) -> WorkflowBudget:
    async with services.sessionmaker() as session:
        return await workflow_budget(
            services.quota,
            session,
            tenant_id="gct",
            application_id=APP,
            workflow_id=wf,
            domains=[ILLUSTRATIVE],
        )


async def step(app_client, payload: dict[str, Any]) -> dict[str, Any]:
    async with app_client(APP) as client:
        body = (await client.post("/v1/ai/chat", json=payload)).json()
    assert body["status"] == "completed", body.get("error")
    return body


async def test_steps_draw_on_the_default_template(app_client, services) -> None:
    wf = workflow()
    untouched = await readout(services, wf)
    assert untouched.budget is not None and untouched.budget.id == TEMPLATE
    assert untouched.cap_micros == 1_000_000 and untouched.on_exhaustion == "approval_required"
    assert (untouched.spent_micros, untouched.reserved_micros) == (0, 0)
    # Simulated steps run in the illustrative domain, whose figures are never labelled measured.
    assert untouched.remaining_micros == 1_000_000 and untouched.provenance is Provenance.SIMULATED

    committed = [(await step(app_client, metered(wf)))["budget"]["committed_micros"] for _ in range(2)]
    after = await readout(services, wf)
    assert after.scope_id == f"{APP}/{wf}"
    assert after.spent_micros == sum(committed) > 0 and after.reserved_micros == 0
    assert after.remaining_micros == 1_000_000 - sum(committed)
    assert after.provenance is Provenance.SIMULATED, "simulated spend is never shown as measured"


async def test_a_zero_cost_simulated_step_still_labels_the_readout(app_client, services) -> None:
    wf = workflow()
    body = await step(app_client, chat_body("What are the Europe roaming rates?", workflow_id=wf))
    assert body["budget"]["committed_micros"] == 0
    budget = await readout(services, wf)
    assert budget.spent_micros == 0 and budget.provenance is Provenance.SIMULATED


async def test_own_budget_overrides_the_template_and_remaining_is_never_negative(
    app_client, services, finance
) -> None:
    wf = workflow()
    own = await set_budget(finance, wf, 10**9)
    committed = (await step(app_client, metered(wf)))["budget"]["committed_micros"]
    assert committed > 0
    await set_budget(finance, wf, 1)  # cap lowered below what was already spent

    budget = await readout(services, wf)
    assert budget.budget is not None and budget.budget.id == own["id"] != TEMPLATE
    assert budget.cap_micros == 1 and budget.spent_micros == committed
    assert budget.remaining_micros == 0
