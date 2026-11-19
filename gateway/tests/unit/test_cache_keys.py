"""Response-cache key scoping and semantic eligibility (LLD §11 guardrails)."""

from typing import Any

from aiva_gateway.auth.principal import RunIdentity
from aiva_gateway.catalog.models import Application
from aiva_gateway.contracts.chat import ChatRequest
from aiva_gateway.contracts.policy import EffectiveConstraints, PolicyClassification, PolicyDecision
from aiva_gateway.core.enums import Classification, IdentityMode, PolicyAction, SourceMode
from aiva_gateway.optimize.cache import (
    NOT_CACHEABLE_PER_USER_APP,
    NOT_CACHEABLE_PERSONAL,
    build_cache_key,
    entity_signature,
    is_first_person,
)
from aiva_gateway.pipeline.context import RunContext
from aiva_gateway.pipeline.interfaces import DlpFinding, DlpResult

MODELS = ["gemini-flash-lite", "deepseek-v4-1-flash"]


def decision(bundle_hash: str = "bundle-1", models: list[str] = MODELS) -> PolicyDecision:
    return PolicyDecision(
        action=PolicyAction.ALLOW,
        effective=EffectiveConstraints(allowed_models=models),
        bundle_hash=bundle_hash,
        policy_version="1",
    )


def run(
    content: str | list[dict[str, str]],
    *,
    user: str | None = None,
    roles: tuple[str, ...] = (),
    classification: Classification = Classification.INTERNAL,
    findings: tuple[str, ...] = (),
    injection: bool = False,
    evidence: list[dict[str, str]] | None = None,
    tags: list[str] | None = None,
    business_function: str | None = None,
    sharing: str | None = None,
) -> RunContext:
    messages = [{"role": "user", "content": content}] if isinstance(content, str) else content
    request = ChatRequest.model_validate(
        {"messages": messages, "evidence": evidence or [], "routing": {"tags": tags or []}}
    )
    ctx = RunContext(
        run_id="run",
        tenant_id="gct",
        request=request,
        identity=RunIdentity(
            application_id="gct-support-chatbot",
            identity_mode=IdentityMode.AUTHENTICATED,
            source_ip="10.20.1.15",
            source_mode=SourceMode.OBSERVED,
            principal_kind="user" if user else "application",
            principal_pseudonym=user,
            roles=roles,
        ),
        application=Application(
            id="gct-support-chatbot",
            name="Customer Support Chatbot",
            department_id="customer-care",
            trusted_network_ids=[],
            allowed_tasks=[],
            allowed_models=[],
            cache_sharing=sharing,
        )
        if sharing
        else None,
        scenario=None,
        profile=None,
        department_id="customer-care",
        cost_center_id="cc-care",
        business_function=business_function,
        environment="test",
        data_designation="synthetic",
        request_hash="h",
        messages=messages,
        original_context_tokens=10,
    )
    ctx.classification = PolicyClassification(effective=classification)
    ctx.dlp = DlpResult(
        findings=tuple(DlpFinding(t, Classification.CONFIDENTIAL, 0, 1) for t in findings),
        injection_indicators=("override-instructions",) if injection else (),
    )
    ctx.evidence_dlp = DlpResult()
    return ctx


def key(ctx: RunContext, **kwargs: Any):  # type: ignore[no-untyped-def]
    kwargs.setdefault("knowledge_version", "kb-1")
    found, _ = build_cache_key(ctx, kwargs.pop("decision", decision()), **kwargs)
    return found


GENERIC = "What are the synthetic roaming rates for Europe on the Traveller 10 bundle?"


def test_generic_requests_share_within_application_and_role_set() -> None:
    a, b = key(run(GENERIC, user="p-alice")), key(run(GENERIC, user="p-bob"))
    assert a is not None and b is not None and a.key_hash == b.key_hash
    assert key(run(GENERIC, user="p-alice", roles=("tester",))).key_hash != a.key_hash


def test_account_specific_requests_are_per_user_and_never_semantic() -> None:
    prompt = "Why did my bill increase by 15 AED this month?"
    alice, bob = key(run(prompt, user="p-alice")), key(run(prompt, user="p-bob"))
    assert alice.key_hash != bob.key_hash and "user:p-alice" in alice.scope
    assert alice.semantic is None and bob.semantic is None
    found, reason = build_cache_key(run(prompt), decision(), knowledge_version="kb-1")
    assert found is None and reason == NOT_CACHEABLE_PERSONAL  # no end-user identity: not cached


def test_identifiers_fraud_and_confidential_data_are_personal() -> None:
    assert key(run(GENERIC, findings=("MSISDN",))) is None
    assert key(run(GENERIC, tags=["fraud-investigation"])) is None
    assert key(run(GENERIC, business_function="fraud")) is None
    assert key(run(GENERIC, classification=Classification.CONFIDENTIAL)) is None
    scoped = key(run(GENERIC, user="p-alice", findings=("MSISDN",)))
    assert scoped is not None and scoped.semantic is None and "user:p-alice" in scoped.scope


def test_policy_kb_model_pool_and_evidence_content_change_the_key() -> None:
    base = key(run(GENERIC)).key_hash
    assert key(run(GENERIC), decision=decision(bundle_hash="bundle-2")).key_hash != base
    assert key(run(GENERIC), knowledge_version="kb-2").key_hash != base
    assert key(run(GENERIC), decision=decision(models=["claude-flagship"])).key_hash != base
    first = key(run(GENERIC, evidence=[{"id": "e1", "text": "Europe: 0.25 AED/min"}]))
    second = key(run(GENERIC, evidence=[{"id": "e1", "text": "Europe: 0.99 AED/min"}]))
    assert first.key_hash != second.key_hash  # same id, different content: no reuse


def test_semantic_reuse_only_for_single_turn_clean_requests() -> None:
    assert key(run(GENERIC)).semantic is not None
    assert key(run(GENERIC, evidence=[{"id": "e1", "text": "x"}])).semantic is None
    assert key(run(GENERIC, injection=True)).semantic is None
    multi = [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello"},
        {"role": "user", "content": GENERIC},
    ]
    assert key(run(multi)).semantic is None


def test_entity_signature_is_the_factual_equivalence_check() -> None:
    paraphrase = "List the synthetic roaming rates for Europe on the Traveller 10 bundle."
    assert entity_signature(GENERIC) == entity_signature(paraphrase)
    assert entity_signature(GENERIC) != entity_signature(GENERIC.replace("Europe", "Asia"))
    assert entity_signature(GENERIC) != entity_signature(GENERIC.replace("10", "20"))
    assert entity_signature("Which plans include roaming?") != entity_signature(
        "Which plans do not include roaming?"
    )
    assert key(run(GENERIC)).semantic.bucket == key(run(paraphrase)).semantic.bucket


# Personal phrasings that named no account noun right after "my" and so used to be shared (cross-user leak).
FIRST_PERSON_PROMPTS = (
    "Why was I charged 15 AED extra this month?",
    "How much do I owe GCT right now?",
    "When does my prepaid plus renew?",
    "Is the roaming charge on my latest statement correct?",
    "Can you tell me why my monthly Prepaid Plus fee went up?",
    # PR #22's cases
    "Why did my synthetic GCT Prepaid Plus bill increase by 15 AED this month?",
    "Why did my synthetic GCT bill increase by 15 AED this month?",
    "Can you check our corporate roaming data usage?",
    # contractions, including a typographic apostrophe
    "I'm being charged twice for roaming.",
    "We\u2019ve moved to the Business plan, what changes?",
    "Help us understand the fee.",
)
GENERIC_PROMPTS = (
    "What roaming plans does GCT offer?",
    GENERIC,
    "How is the Prepaid Plus digital services fee calculated?",
    "Which bundles include unlimited SMS?",
    "Explain the enterprise roaming discount approval process.",
    "Is 5G available on the Traveller 10 bundle in Asia?",
    "What bonus data comes with the Business plan?",
)


def test_first_person_requests_are_personal() -> None:
    for prompt in FIRST_PERSON_PROMPTS:
        assert is_first_person(prompt), prompt
        alice, bob = key(run(prompt, user="p-alice")), key(run(prompt, user="p-bob"))
        assert alice.key_hash != bob.key_hash and "user:p-alice" in alice.scope, prompt
        assert alice.semantic is None and bob.semantic is None, prompt
        found, reason = build_cache_key(run(prompt), decision(), knowledge_version="kb-1")
        assert found is None and reason == NOT_CACHEABLE_PERSONAL, prompt


def test_generic_questions_still_share() -> None:
    for prompt in GENERIC_PROMPTS:
        assert not is_first_person(prompt), prompt
        alice, bob = key(run(prompt, user="p-alice")), key(run(prompt, user="p-bob"))
        assert alice.key_hash == bob.key_hash and alice.semantic is not None, prompt


def test_per_user_applications_never_share_but_keep_same_user_reuse() -> None:
    alice = key(run(GENERIC, user="p-alice", sharing="per_user"))
    bob = key(run(GENERIC, user="p-bob", sharing="per_user"))
    assert alice.key_hash != bob.key_hash and alice.semantic.bucket != bob.semantic.bucket
    assert alice.key_hash == key(run(GENERIC, user="p-alice", sharing="per_user")).key_hash
    found, reason = build_cache_key(run(GENERIC, sharing="per_user"), decision(), knowledge_version="kb-1")
    assert found is None and reason == NOT_CACHEABLE_PER_USER_APP
    assert (
        key(run(GENERIC, user="p-alice", sharing="shared")).key_hash
        == key(run(GENERIC, user="p-bob")).key_hash
    )


def test_answers_never_cross_execution_modes() -> None:
    # ADR-0019: a replayed or simulated answer is never served to a live run, nor a live one to a replay run,
    # neither exactly nor semantically (the semantic match requires the same scope).
    keys = {}
    for mode in ("replay", "live", "simulated"):
        ctx = run(GENERIC)
        ctx.execution_mode = mode
        keys[mode] = key(ctx)
    assert len({k.key_hash for k in keys.values()}) == 3
    assert len({k.scope for k in keys.values()}) == 3
    assert all(k.semantic is not None for k in keys.values())
