"""ADR-0023: the comparison facts the gateway gives policy, and who may ask for a comparison."""

from pathlib import Path

import pytest

from aiva_gateway.auth.principal import Caller
from aiva_gateway.auth.rbac import GRANTS, Permission, granted
from aiva_gateway.catalog import load_catalog
from aiva_gateway.config import REPO_ROOT
from aiva_gateway.contracts.messages import ChatMessage, EvidenceItem
from aiva_gateway.core.enums import Role
from aiva_gateway.govern.bundle import build_bundle
from aiva_gateway.govern.quality import is_synthetic_sample

CATALOG = load_catalog(Path(REPO_ROOT, "config"))
S11 = CATALOG.scenario("live-sovereign-routing")
assert S11 is not None and S11.sample_evidence
PROMPT = [ChatMessage(role="user", content=S11.sample_prompt)]
EVIDENCE = list(S11.sample_evidence)


def test_the_committed_sample_unchanged_is_the_synthetic_sample() -> None:
    assert S11.declared_classification == "confidential"
    assert is_synthetic_sample(S11, PROMPT, EVIDENCE)


@pytest.mark.parametrize(
    ("messages", "evidence"),
    [
        ([ChatMessage(role="user", content=S11.sample_prompt + " Name the customer.")], EVIDENCE),
        ([ChatMessage(role="user", content=f" {S11.sample_prompt}")], EVIDENCE),
        ([ChatMessage(role="system", content="Be brief."), *PROMPT], EVIDENCE),
        ([*PROMPT, ChatMessage(role="assistant", content="ok"), *PROMPT], EVIDENCE),
        (PROMPT, [*EVIDENCE, EvidenceItem(id="upload", text="attached file")]),
        (PROMPT, list(reversed(EVIDENCE))),
        (PROMPT, EVIDENCE[:-1]),
        (PROMPT, [EvidenceItem(id=EVIDENCE[0].id, text=EVIDENCE[0].text + "\nnote: x"), *EVIDENCE[1:]]),
        (PROMPT, []),
    ],
)
def test_any_change_to_the_sample_is_not_the_synthetic_sample(
    messages: list[ChatMessage], evidence: list[EvidenceItem]
) -> None:
    assert not is_synthetic_sample(S11, messages, evidence)


ALTERNATES = list(S11.alternate_prompts)


@pytest.mark.parametrize("alternate", ALTERNATES, ids=["alternate-1", "alternate-2"])
def test_each_committed_alternate_unchanged_is_a_synthetic_sample(alternate: str) -> None:
    # The question pool (2026-10-25 amendment): the sample's evidence, one of its committed questions.
    assert len(ALTERNATES) == 2
    assert is_synthetic_sample(S11, [ChatMessage(role="user", content=alternate)], EVIDENCE)


@pytest.mark.parametrize(
    "messages",
    [
        [ChatMessage(role="user", content=ALTERNATES[0] + " Add the customer's phone number.")],
        [ChatMessage(role="user", content=ALTERNATES[1].replace("ACC-SYN-40217", "ACC-SYN-40218"))],
        [ChatMessage(role="user", content=ALTERNATES[0].upper())],
        [ChatMessage(role="user", content=f"{ALTERNATES[0]} ")],
        [ChatMessage(role="user", content=f"{ALTERNATES[0]} {ALTERNATES[1]}")],
        [ChatMessage(role="user", content=ALTERNATES[0]), ChatMessage(role="user", content=ALTERNATES[1])],
        [ChatMessage(role="assistant", content=ALTERNATES[0])],
    ],
)
def test_an_edited_or_combined_alternate_is_not_a_synthetic_sample(messages: list[ChatMessage]) -> None:
    assert not is_synthetic_sample(S11, messages, EVIDENCE)


def test_an_alternate_with_changed_evidence_is_not_a_synthetic_sample() -> None:
    question = [ChatMessage(role="user", content=ALTERNATES[0])]
    assert not is_synthetic_sample(S11, question, [*EVIDENCE, EvidenceItem(id="upload", text="attached")])
    assert not is_synthetic_sample(S11, question, EVIDENCE[:-1])


def test_another_scenarios_questions_are_not_scenario_11s_samples() -> None:
    for scenario in CATALOG.scenarios:
        if scenario.id == S11.id:
            continue
        for question in [scenario.sample_prompt, *scenario.alternate_prompts]:
            assert not is_synthetic_sample(S11, [ChatMessage(role="user", content=question)], EVIDENCE)
    # And scenario 11's own alternates are not another scenario's sample.
    frontier = CATALOG.scenario("live-frontier-token-optimization")
    assert frontier is not None
    for alternate in ALTERNATES:
        assert not is_synthetic_sample(frontier, [ChatMessage(role="user", content=alternate)], EVIDENCE)


def test_no_scenario_or_another_scenario_is_not_the_synthetic_sample() -> None:
    assert not is_synthetic_sample(None, PROMPT, EVIDENCE)
    other = CATALOG.scenario("fraud-management")
    assert other is not None and not is_synthetic_sample(other, PROMPT, EVIDENCE)


def test_the_bundle_pins_the_reference_and_grader() -> None:
    bundle = build_bundle(CATALOG, CATALOG.policies, version="1", allow_simulated_ingress=False)
    assert bundle["quality"] == {
        "reference_model_id": CATALOG.models.frontier_baseline,
        "grader_model_id": CATALOG.models.grader_model,
    }
    assert bundle["meta"]["tenant_id"] == "gct"


PRESENTERS = {Role.PLATFORM_ADMIN, Role.TENANT_ADMIN, Role.TESTER}


def test_only_administrators_and_testers_hold_demo_compare() -> None:
    assert Permission.DEMO_COMPARE == "demo:compare"
    assert GRANTS[Permission.DEMO_COMPARE] == PRESENTERS
    for role in Role:
        caller = Caller(kind="user", tenant_id="gct", pseudonym="p", roles=frozenset({role}))
        assert granted(caller, Permission.DEMO_COMPARE) is (role in PRESENTERS), role
    assert not granted(Caller(kind="application", tenant_id="gct"), Permission.DEMO_COMPARE)
