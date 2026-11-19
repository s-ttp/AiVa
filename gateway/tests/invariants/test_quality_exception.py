"""AIPEL-INV-001 has no exception for answer-quality comparisons, against the real policy engine and bundle.

The reference deployment allowed one carve-out (ADR-0023): a presenter's comparison of the live
sovereign-routing scenario's committed synthetic sample with the frontier reference. This build does not:
confidential data never reaches the reference or the grader, whoever asks and whatever the sample. The
served answer still takes the sovereign path. The Appendix B tests in `test_appendix_b.py` are unchanged.
"""

from dataclasses import replace
from typing import Any

from aiva_gateway.auth.principal import RunIdentity
from aiva_gateway.contracts.chat import ChatRequest
from aiva_gateway.contracts.policy import PolicyDecision, PolicyEgress, PolicyInput
from aiva_gateway.core.enums import (
    AttemptStage,
    Classification,
    IdentityMode,
    PolicyAction,
    PolicyStage,
    SourceMode,
)
from aiva_gateway.govern.bundle import build_bundle
from aiva_gateway.govern.policy_input import build_policy_input
from aiva_gateway.govern.quality import quality_facts
from aiva_gateway.pipeline.context import RunContext
from aiva_gateway.pipeline.orchestrator import Orchestrator
from aiva_gateway.services import Services
from tests.conftest import CONSOLE_IP

S11 = "live-sovereign-routing"
STAND_IN_GRADER = "test-grader"
PRESENTER = RunIdentity(
    application_id="gct-support-chatbot",
    identity_mode=IdentityMode.SIMULATED,
    source_ip=CONSOLE_IP,
    source_mode=SourceMode.SIMULATED,
    principal_kind="user",
    principal_pseudonym="usr_presenter",
    roles=("tester",),
)


def _sample_request(services: Services, **changes: Any) -> ChatRequest:
    """S11's default request, as the live page sends it (`run/draft.ts`)."""
    scenario = services.catalog.scenario(S11)
    assert scenario is not None and scenario.sample_evidence
    body: dict[str, Any] = {
        "messages": [{"role": "user", "content": scenario.sample_prompt}],
        "routing": {"mode": "hybrid", "tags": scenario.routing_tags},
        "data_classification": scenario.declared_classification,
        "scenario_id": S11,
        "evidence": [e.model_dump() for e in scenario.sample_evidence],
    }
    return ChatRequest.model_validate({**body, **changes})


async def _served(services: Services, request: ChatRequest, identity: RunIdentity = PRESENTER) -> RunContext:
    orchestrator = Orchestrator(services)
    ctx = orchestrator.new_context(request, identity, "gct")
    await orchestrator.run(ctx)
    return ctx


def _bundle(services: Services, **quality: Any) -> dict[str, Any]:
    """The real bundle from the catalog and policies, with the catalog's grader, or a stand-in until one is
    registered (Optimize pins it in `config/models.yaml`)."""
    bundle = build_bundle(
        services.catalog, services.catalog.policies, version="test", allow_simulated_ingress=True
    )
    if bundle["quality"]["grader_model_id"] is None:
        bundle["models"][STAND_IN_GRADER] = {
            "role": "grader",
            "external": True,
            "sovereign": False,
            "enabled": True,
        }
        bundle["quality"]["grader_model_id"] = STAND_IN_GRADER
    bundle["quality"].update(quality)
    return bundle


def _comparison(
    ctx: RunContext, stage: AttemptStage, model_id: str, *, requested: bool = True
) -> PolicyInput:
    return build_policy_input(
        ctx,
        PolicyStage.EGRESS,
        egress=PolicyEgress(model_id=model_id, stage=stage),
        quality=quality_facts(ctx) if requested else None,
    )


async def _decide(services: Services, policy_input: PolicyInput, **quality: Any) -> PolicyDecision:
    return await services.policy_engine.simulate(_bundle(services, **quality), policy_input)


def _rules(decision: PolicyDecision) -> set[str]:
    return {r.rule_id for r in decision.reasons}


def _reference(services: Services) -> str:
    return services.catalog.models.frontier_baseline


def _grader(services: Services) -> str:
    return services.catalog.models.grader_model or STAND_IN_GRADER


async def _both(services: Services, ctx: RunContext, **update: Any) -> list[PolicyDecision]:
    """The reference and grading decisions for `ctx`, with fields of the policy input replaced."""
    decisions = []
    for stage, model_id in (
        (AttemptStage.QUALITY_REFERENCE, _reference(services)),
        (AttemptStage.QUALITY_GRADING, _grader(services)),
    ):
        policy_input = _comparison(ctx, stage, model_id)
        decisions.append(await _decide(services, policy_input.model_copy(update=update)))
    return decisions


async def test_the_synthetic_sample_comparison_is_denied_and_the_answer_stays_sovereign(services) -> None:  # type: ignore[no-untyped-def]
    ctx = await _served(services, _sample_request(services))
    # The served answer is unchanged: sovereign direct, the sovereign model only, no CIM.
    assert ctx.request_decision is not None and ctx.request_decision.action == PolicyAction.ALLOW
    assert ctx.classification is not None and ctx.classification.effective == "confidential"
    assert ctx.request_decision.effective.execution_path == "sovereign-direct"
    assert ctx.request_decision.effective.allowed_models == ["gct-qwen-finetuned"]
    assert {a.model.id for a in ctx.attempts} == {"gct-qwen-finetuned"}
    assert {a.stage for a in ctx.attempts} == {AttemptStage.UPSTREAM}

    facts = quality_facts(ctx)
    assert facts.requested and facts.synthetic_sample and facts.data_designation == "synthetic"
    reference, grading = await _both(services, ctx)
    assert reference.action == grading.action == PolicyAction.DENY, (reference.reasons, grading.reasons)
    assert "AIPEL-INV-001" in _rules(reference) and "AIPEL-INV-001" in _rules(grading)
    # The bundle the gateway published denies it too.
    loaded = await services.policy_engine.evaluate(
        _comparison(ctx, AttemptStage.QUALITY_REFERENCE, _reference(services))
    )
    assert loaded.action == PolicyAction.DENY and "AIPEL-INV-001" in _rules(loaded), loaded.reasons


async def test_each_committed_alternate_is_denied_and_stays_sovereign(services) -> None:  # type: ignore[no-untyped-def]
    """A committed alternate over the unchanged evidence is a synthetic sample. It is served on the sovereign
    model alone, and nothing about it leaves, the comparison included."""
    scenario = services.catalog.scenario(S11)
    assert scenario is not None and len(scenario.alternate_prompts) == 2
    for alternate in scenario.alternate_prompts:
        ctx = await _served(
            services, _sample_request(services, messages=[{"role": "user", "content": alternate}])
        )
        assert ctx.request_decision is not None and ctx.request_decision.action == PolicyAction.ALLOW
        assert ctx.request_decision.effective.execution_path == "sovereign-direct"
        assert ctx.request_decision.effective.allowed_models == ["gct-qwen-finetuned"]
        assert {(a.stage, a.model.id) for a in ctx.attempts} == {
            (AttemptStage.UPSTREAM, "gct-qwen-finetuned")
        }
        assert quality_facts(ctx).synthetic_sample, alternate
        reference, grading = await _both(services, ctx)
        assert reference.action == grading.action == PolicyAction.DENY, (reference.reasons, grading.reasons)
        assert "AIPEL-INV-001" in _rules(reference) and "AIPEL-INV-001" in _rules(grading)
        for stage in (AttemptStage.UPSTREAM, AttemptStage.BASELINE):
            decision = await _decide(services, _comparison(ctx, stage, _reference(services)))
            assert decision.action == PolicyAction.DENY and "AIPEL-INV-001" in _rules(decision)


async def test_every_other_egress_of_the_sample_is_still_denied(services) -> None:  # type: ignore[no-untyped-def]
    ctx = await _served(services, _sample_request(services))
    for stage in (AttemptStage.UPSTREAM, AttemptStage.BASELINE, AttemptStage.VALIDATION):
        # Even with the comparison facts present, only the two comparison stages are excepted.
        decision = await _decide(services, _comparison(ctx, stage, _reference(services)))
        assert decision.action == PolicyAction.DENY
        assert {"AIPEL-INV-001", "AIPEL-EGRESS-001"} <= _rules(decision)
    cim = await _decide(services, _comparison(ctx, AttemptStage.CIM, services.catalog.models.cim_model))
    assert cim.action == PolicyAction.DENY and "AIPEL-EGRESS-002" in _rules(cim)


async def test_without_an_explicit_presenter_request_both_calls_are_denied(services) -> None:  # type: ignore[no-untyped-def]
    ctx = await _served(services, _sample_request(services))
    for stage, model_id in (
        (AttemptStage.QUALITY_REFERENCE, _reference(services)),
        (AttemptStage.QUALITY_GRADING, _grader(services)),
    ):
        decision = await _decide(services, _comparison(ctx, stage, model_id, requested=False))
        assert decision.action == PolicyAction.DENY
        assert {"AIPEL-QUAL-001", "AIPEL-INV-001"} <= _rules(decision)


async def test_a_visitor_or_an_application_never_gets_the_exception(services) -> None:  # type: ignore[no-untyped-def]
    ctx = await _served(services, _sample_request(services))
    base = _comparison(ctx, AttemptStage.QUALITY_REFERENCE, _reference(services)).identity
    for identity in (
        base.model_copy(update={"roles": ["public_visitor"]}),
        base.model_copy(update={"roles": ["tester", "public_visitor"]}),
        base.model_copy(update={"roles": ["security_auditor"]}),
        base.model_copy(update={"kind": "application", "roles": []}),
    ):
        for decision in await _both(services, ctx, identity=identity):
            assert decision.action == PolicyAction.DENY, identity
            assert {"AIPEL-QUAL-001", "AIPEL-INV-001"} <= _rules(decision)


async def test_text_other_than_the_committed_sample_is_never_sent(services) -> None:  # type: ignore[no-untyped-def]
    scenario = services.catalog.scenario(S11)
    assert scenario is not None and scenario.sample_evidence
    first = scenario.sample_evidence[0]
    alternate = scenario.alternate_prompts[0]
    frontier = services.catalog.scenario("live-frontier-token-optimization")
    assert frontier is not None and frontier.alternate_prompts
    edits: list[dict[str, Any]] = [
        {"messages": [{"role": "user", "content": scenario.sample_prompt + " Include the customer's name."}]},
        {"messages": [{"role": "user", "content": alternate + " Include the customer's phone number."}]},
        {"messages": [{"role": "user", "content": frontier.alternate_prompts[0]}]},
        {
            "messages": [{"role": "user", "content": alternate}],
            "evidence": [*(e.model_dump() for e in scenario.sample_evidence), {"id": "upload", "text": "x"}],
        },
        {"evidence": [*(e.model_dump() for e in scenario.sample_evidence), {"id": "upload", "text": "x"}]},
        {"evidence": [{"id": first.id, "text": first.text + "\nnotes: extra"}]},
    ]
    for edit in edits:
        ctx = await _served(services, _sample_request(services, **edit))
        assert not quality_facts(ctx).synthetic_sample
        for decision in await _both(services, ctx):
            assert decision.action == PolicyAction.DENY, edit
            assert "AIPEL-INV-001" in _rules(decision)


async def test_another_confidential_scenario_never_gets_the_exception(services) -> None:  # type: ignore[no-untyped-def]
    request = ChatRequest.model_validate(
        {
            "messages": [{"role": "user", "content": "Investigate synthetic SIM-swap alert FR-2026-0412"}],
            "scenario_id": "fraud-management",
            "data_classification": "confidential",
        }
    )
    identity = replace(PRESENTER, application_id="gct-fraud-investigator")
    ctx = await _served(services, request, identity)
    reference, grading = await _both(services, ctx)
    assert reference.action == grading.action == PolicyAction.DENY
    assert {"AIPEL-INV-001", "AIPEL-EGRESS-001"} <= _rules(reference)
    assert {"AIPEL-INV-001", "AIPEL-QUAL-003"} <= _rules(grading)


async def test_each_remaining_gate_is_required(services) -> None:  # type: ignore[no-untyped-def]
    ctx = await _served(services, _sample_request(services))
    policy_input = _comparison(ctx, AttemptStage.QUALITY_REFERENCE, _reference(services))
    assert policy_input.quality is not None
    restricted = policy_input.classification.model_copy(
        update={"dlp": Classification.RESTRICTED, "effective": Classification.RESTRICTED}
    )
    for name, update in {
        "deployment not synthetic": {
            "quality": policy_input.quality.model_copy(update={"data_designation": "customer"})
        },
        "another tenant": {"tenant_id": "acme"},
        "another scenario": {"request": policy_input.request.model_copy(update={"scenario_id": None})},
        "restricted": {"classification": restricted},
        "local-only routing": {
            "request": policy_input.request.model_copy(update={"routing_mode": "local-only"})
        },
    }.items():
        for decision in await _both(services, ctx, **update):
            assert decision.action == PolicyAction.DENY, name
            assert "AIPEL-INV-001" in _rules(decision), name


async def test_only_the_pinned_reference_and_grader_may_be_called(services) -> None:  # type: ignore[no-untyped-def]
    ctx = await _served(services, _sample_request(services))
    wrong_reference = await _decide(
        services, _comparison(ctx, AttemptStage.QUALITY_REFERENCE, "gemini-flash-lite")
    )
    wrong_grader = await _decide(
        services, _comparison(ctx, AttemptStage.QUALITY_GRADING, _reference(services))
    )
    no_grader = await _decide(
        services, _comparison(ctx, AttemptStage.QUALITY_GRADING, _grader(services)), grader_model_id=None
    )
    for decision in (wrong_reference, wrong_grader, no_grader):
        assert decision.action == PolicyAction.DENY
        assert {"AIPEL-QUAL-002", "AIPEL-INV-001"} <= _rules(decision)
