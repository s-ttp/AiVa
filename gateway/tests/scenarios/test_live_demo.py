"""The live demo page's use cases (scenarios 11-16): each default request lights the route its slide shows.

They are sent exactly as the page sends them, built from the catalog: prompt, evidence, tier, output budget,
declared classification. The page runs them live, so they have no recordings: inference here is simulated.
Routing is the heuristic RLCD's (CI has no ModernBERT); the routes below do not depend on its predictions:
policy, the quality tier and the rate card decide them.

- 11: confidential customer data takes the sovereign direct path; every external model is denied.
- 12: a large internal knowledge pack at the critical tier goes through the CIM to the frontier model, and
  the same request again is a response-cache hit.
- 13: a prompt-injection and exfiltration attempt is blocked by policy before any model call.
- 14: public data on the public web assistant goes to the cheapest qualifying API model.
- 15: a market research question at the high tier: retrieval, the CIM, then the cheapest model of at least
  DeepSeek's quality.
- 16: a coding task at the critical tier goes to the frontier model with its full context: coding is not a
  task the CIM was trained for, so the CIM is skipped.

Which model is cheapest is the rate card's call (Gemini's pin and price may change), so 14 and 15 expect the
router's own rule: the lowest estimated cost per request, GPU time included, among the allowed models that
meet the tier.

The page asks each use case's sample question or one of its two alternates, picked at random (owner decision,
2026-10-25), so every route test runs over the whole pool: each question must light the same route.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from aiva_gateway.catalog import load_catalog
from aiva_gateway.catalog.models import MIN_QUALITY_RANK, Grounding
from aiva_gateway.config import REPO_ROOT, Settings
from aiva_gateway.contracts.cim import CimEvidence
from aiva_gateway.db.models import ModelAttemptRow
from aiva_gateway.finops.pricing import PriceBook
from aiva_gateway.inference.tokens import input_token_bound
from aiva_gateway.optimize.cim_live import TASK_OUTSIDE_TRAINING
from aiva_gateway.optimize.rlcd import request_costs
from aiva_gateway.optimize.web_search import TavilySearch
from aiva_gateway.pipeline.orchestrator import CIM_BYPASS_MESSAGES, full_context_messages
from aiva_gateway.services import Services, build_services
from tests.conftest import console, with_overrides
from tests.scenarios.conftest import EXTERNAL, with_cache

CATALOG = load_catalog(REPO_ROOT / "config")


def _pool(scenario_id: str) -> list[str]:
    """The questions the page may ask for a use case: its sample, then its alternates."""
    scenario = CATALOG.scenario(scenario_id)
    assert scenario is not None and len(scenario.alternate_prompts) == 2
    return [scenario.sample_prompt, *scenario.alternate_prompts]


def _each_question(scenario_id: str) -> Any:
    return pytest.mark.parametrize(
        "question", _pool(scenario_id), ids=["sample", "alternate-1", "alternate-2"]
    )


@pytest.fixture(scope="module")
async def live_services(settings: Settings, scenario_kb: None) -> AsyncIterator[Services]:
    svc = build_services(
        settings.model_copy(update={"inference_mode": "simulated", "response_cache_enabled": False})
    )
    yield svc
    await svc.aclose()


def _cheapest_qualifying(scenario_id: str, grounding: Grounding = "none") -> str:
    """The router's choice for a scenario on the cim-assisted path: among the application's allowed models
    that meet its tier and answer its grounding, the lowest estimated cost per request, GPU time included
    (`optimize.rlcd`)."""
    scenario = CATALOG.scenario(scenario_id)
    assert scenario is not None
    application = next(a for a in CATALOG.tenant.applications if a.id == scenario.application_id)
    models = [m for m in CATALOG.models.models if m.id in application.allowed_models]
    qualified = [
        m
        for m in models
        if m.quality_rank >= MIN_QUALITY_RANK[scenario.quality_tier] and m.answers(grounding)
    ]
    evidence = [CimEvidence(id=e.id, text=e.text) for e in scenario.sample_evidence or []]
    messages = [{"role": "user", "content": scenario.sample_prompt or ""}]
    costs = request_costs(
        PriceBook(CATALOG),
        qualified,
        input_tokens=input_token_bound(full_context_messages(messages, evidence)),
        max_output_tokens=scenario.max_output_tokens,
    )
    best = min(qualified, key=lambda m: (costs[m.id], -m.quality_rank, m.id))
    return best.id


def _request(scenario_id: str, question: str | None = None) -> dict[str, Any]:
    scenario = CATALOG.scenario(scenario_id)
    assert scenario is not None and scenario.live_demo_use_case
    body: dict[str, Any] = {
        "messages": [{"role": "user", "content": question or scenario.sample_prompt}],
        "routing": {"mode": "hybrid", "tags": scenario.routing_tags},
        "data_classification": scenario.declared_classification,
        "quality_tier": scenario.quality_tier,
        "max_output_tokens": scenario.max_output_tokens,
        "scenario_id": scenario.id,
        "trace": True,
        **console(scenario.application_id),
    }
    if scenario.sample_evidence:
        body["evidence"] = [e.model_dump() for e in scenario.sample_evidence]
    return body


async def _models(db, run_id: str) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    rows = await db.execute(select(ModelAttemptRow).where(ModelAttemptRow.run_id == run_id))
    return [(a.stage, a.model_id) for a in rows.scalars()]


def test_the_live_use_cases_are_the_six_on_the_page() -> None:
    live = {s.live_demo_use_case: s for s in CATALOG.scenarios if s.live_demo_use_case}
    assert {case: s.number for case, s in live.items()} == {
        "sovereign-routing": 11,
        "frontier-optimization": 12,
        "policy-enforcement": 13,
        "inference-api": 14,
        "market-research": 15,
        "code-generation": 16,
    }
    assert live["sovereign-routing"].declared_classification == "confidential"
    frontier = live["frontier-optimization"]
    assert (frontier.quality_tier, frontier.task_category) == ("critical", "rag")
    assert live["inference-api"].application_id == "gct-public-web-assistant"
    assert (live["market-research"].application_id, live["market-research"].quality_tier) == (
        "gct-market-intelligence",
        "high",
    )
    code = live["code-generation"]
    assert (code.application_id, code.task_category, code.quality_tier) == (
        "gct-coding-assistant",
        "coding",
        "critical",
    )
    assert code.declared_classification == "internal" and not code.sample_evidence
    application = next(a for a in CATALOG.tenant.applications if a.id == "gct-public-web-assistant")
    assert application.classification_floor == "public" and not {"gct-qwen-finetuned"} & set(
        application.allowed_models
    )


def test_every_question_in_the_pools_is_clean_and_the_sovereign_ones_stay_on_the_samples_customer(
    live_services,
) -> None:  # type: ignore[no-untyped-def]
    """DLP finds nothing in any question (scenario 13's are injection attempts, which its own screening
    catches). Scenario 11's alternates ask about the sample's synthetic customer only, over its unchanged
    evidence."""
    for scenario in CATALOG.scenarios:
        for question in _pool(scenario.id) if scenario.live_demo_use_case else []:
            assert live_services.dlp.scan(question).finding_types == [], (scenario.id, question)
    sovereign = CATALOG.scenario("live-sovereign-routing")
    assert sovereign is not None and sovereign.declared_classification == "confidential"
    assert all("ACC-SYN-40217" in q for q in sovereign.alternate_prompts)
    ids = {e.id.split("/")[0] for e in sovereign.sample_evidence or []}
    assert ids == {"ACC-SYN-40217"}


@_each_question("live-sovereign-routing")
async def test_confidential_customer_data_stays_on_the_sovereign_model(
    live_services, tester_client, db, question
) -> None:  # type: ignore[no-untyped-def]
    async with tester_client(svc=live_services) as client:
        body = (await client.post("/v1/ai/chat", json=_request("live-sovereign-routing", question))).json()
    assert body["status"] == "completed", body.get("error")
    assert body["execution_path"] == "sovereign-direct" and body["selected_model"] == "gct-qwen-finetuned"
    assert body["policy"]["effective_classification"] == "confidential"
    assert {c["model_id"] for c in body["policy"]["denied_candidates"]} == EXTERNAL
    assert await _models(db, body["run_id"]) == [("upstream", "gct-qwen-finetuned")]


@_each_question("live-frontier-token-optimization")
async def test_a_large_knowledge_pack_goes_through_the_cim_to_the_frontier_model_then_the_cache(
    live_services, tester_client, db, question
) -> None:  # type: ignore[no-untyped-def]
    svc = with_cache(live_services)
    request = _request("live-frontier-token-optimization", question)
    async with tester_client(svc=svc) as client:
        first = (await client.post("/v1/ai/chat", json=request)).json()
        again = (await client.post("/v1/ai/chat", json=request)).json()
    assert first["status"] == "completed", first.get("error")
    assert first["execution_path"] == "cim-assisted" and first["selected_model"] == "claude-flagship"
    assert first["cim_invoked"] and first["cim"]["passages_retrieved"] >= 65
    stages = await _models(db, first["run_id"])
    assert ("cim", "enterprise-cim") in stages and ("upstream", "claude-flagship") in stages
    assert again["execution_path"] == "cache-return" and again["cache"]["hit"]
    assert await _models(db, again["run_id"]) == []


@_each_question("live-security-enforcement")
async def test_an_injection_attempt_is_blocked_before_any_model(
    live_services, tester_client, db, question
) -> None:  # type: ignore[no-untyped-def]
    """Every question in the pool is refused by the sample's rule, the prompt-injection policy alone, with no
    model attempt of any kind."""
    async with tester_client(svc=live_services) as client:
        body = (await client.post("/v1/ai/chat", json=_request("live-security-enforcement", question))).json()
    assert body["status"] == "blocked" and body["error"]["code"] == "POLICY_DENIED"
    assert body["error"]["rule_ids"] == ["GCT-INJ-003"]
    assert body["response"] is None and await _models(db, body["run_id"]) == []


@_each_question("live-cost-optimized-api")
async def test_public_data_goes_to_the_cheapest_qualifying_api_model(
    live_services, tester_client, db, question
) -> None:  # type: ignore[no-untyped-def]
    async with tester_client(svc=live_services) as client:
        body = (await client.post("/v1/ai/chat", json=_request("live-cost-optimized-api", question))).json()
    assert body["status"] == "completed", body.get("error")
    assert body["policy"]["effective_classification"] == "public"
    # The answer is grounded in retrieved KB passages only, which Gemini Flash-Lite does not qualify to answer
    # (ADR-0009, 2026-10-22 grounded-answers amendment); ungrounded, it would be the cheapest.
    assert body["cim"]["passages_retrieved"] > 0
    cheapest = _cheapest_qualifying("live-cost-optimized-api", "knowledge")
    assert (
        cheapest == "deepseek-v4-1-flash"
        and _cheapest_qualifying("live-cost-optimized-api") == "gemini-flash-lite"
    )
    assert body["selected_model"] == cheapest
    assert ("upstream", cheapest) in await _models(db, body["run_id"])


# A synthetic search result, as the provider would return it (no real page text in the repository).
SEARCH_RESULT = {
    "title": "Gulf roaming day passes compared (synthetic)",
    "url": "https://www.example-telecom.test/roaming/gulf-day-pass?ref=synthetic",
    "raw_content": "A synthetic Gulf operator's roaming day pass costs 35 AED a day with 1 GB of data.",
}


def _searching(svc: Services) -> tuple[Services, list[str]]:
    """Services whose `web.search` answers every query with the synthetic result, and the queries it got."""
    queries: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        queries.append(json.loads(request.content)["query"])
        return httpx.Response(200, json={"results": [SEARCH_RESULT], "usage": {"credits": 1}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    search = TavilySearch(api_key="synthetic-search-key-not-real", http=http, timeout_seconds=1.0)
    return with_overrides(svc, search=search), queries


@_each_question("live-market-research")
async def test_market_research_retrieves_then_the_cim_trims_for_the_cheapest_high_tier_model(
    live_services, tester_client, db, question
) -> None:  # type: ignore[no-untyped-def]
    svc, queries = _searching(live_services)
    async with tester_client(svc=svc) as client:
        body = (await client.post("/v1/ai/chat", json=_request("live-market-research", question))).json()
    assert body["status"] == "completed", body.get("error")
    # The question goes to the web search as it is, and the screened result reaches the answering model.
    assert queries == [question]
    assert body["search"]["status"] == "searched" and body["search"]["results_sent"] == 1
    assert body["execution_path"] == "cim-assisted" and body["cim"]["passages_retrieved"] > 0
    # The high tier rules out rank-1 models; the application allows only DeepSeek and Claude.
    assert _cheapest_qualifying("live-market-research") == "deepseek-v4-1-flash"
    assert body["selected_model"] == "deepseek-v4-1-flash"
    stages = await _models(db, body["run_id"])
    assert ("cim", "enterprise-cim") in stages and ("upstream", "deepseek-v4-1-flash") in stages


@_each_question("live-code-generation")
async def test_a_coding_task_goes_to_the_frontier_model_without_the_cim(
    live_services, tester_client, db, question
) -> None:  # type: ignore[no-untyped-def]
    """Only the frontier model meets the critical tier. Coding is not one of the CIM's trained tasks
    (`cim_trained_tasks`), so the CIM is skipped and the frontier model gets the full context: one upstream
    attempt, no CIM attempt."""
    assert _cheapest_qualifying("live-code-generation") == "claude-flagship"
    async with tester_client(svc=live_services) as client:
        body = (await client.post("/v1/ai/chat", json=_request("live-code-generation", question))).json()
    assert body["status"] == "completed", body.get("error")
    assert body["selected_model"] == "claude-flagship"
    assert body["policy"]["effective_classification"] == "internal"
    assert not body["cim_invoked"]
    assert body["cim"]["message"] == CIM_BYPASS_MESSAGES[TASK_OUTSIDE_TRAINING]
    assert await _models(db, body["run_id"]) == [("upstream", "claude-flagship")]
