"""Policy facts for an answer-quality comparison of a live answer (ADR-0023).

Policy allows the owner's one exception to AIPEL-INV-001 only for the committed synthetic samples of the live
sovereign-routing scenario: its sample question or one of its committed alternates, over its unchanged sample
evidence (2026-10-25 amendment). The gateway decides here whether a run's request is exactly one of them; the
policy checks every other gate.
"""

from collections.abc import Sequence

from aiva_gateway.catalog.models import Scenario
from aiva_gateway.contracts.messages import ChatMessage, EvidenceItem
from aiva_gateway.contracts.policy import PolicyQuality
from aiva_gateway.pipeline.context import RunContext


def is_synthetic_sample(
    scenario: Scenario | None, messages: Sequence[ChatMessage], evidence: Sequence[EvidenceItem]
) -> bool:
    """True only for one of the scenario's committed samples, unchanged: one user message whose text is the
    sample prompt or one of the scenario's committed alternates, and the sample evidence in order. Edited or
    typed text, another scenario's question, an attachment or a reordered passage is not."""
    if scenario is None:
        return False
    questions = {scenario.sample_prompt, *scenario.alternate_prompts}
    sample = [(e.id, e.text) for e in scenario.sample_evidence or []]
    return (
        len(messages) == 1
        and messages[0].role == "user"
        and messages[0].content in questions
        and [(e.id, e.text) for e in evidence] == sample
    )


def quality_facts(ctx: RunContext) -> PolicyQuality:
    """The facts for a presenter's explicit comparison request on the run in `ctx`. Only the quality-check
    endpoint calls this; no other path sets `requested`."""
    return PolicyQuality(
        requested=True,
        synthetic_sample=is_synthetic_sample(ctx.scenario, ctx.request.messages, ctx.request.evidence),
        data_designation=ctx.data_designation,
    )
