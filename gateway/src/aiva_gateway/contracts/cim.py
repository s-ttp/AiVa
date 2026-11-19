"""Generalized CIM interface (LLD §9). Field names follow the LLD exactly."""

from typing import Literal

from pydantic import Field

from aiva_gateway.contracts.common import ContractModel


class CimMessage(ContractModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class CimEvidence(ContractModel):
    id: str = Field(description="Stable evidence reference, e.g. 'doc-1#p4'.")
    text: str


class CimRequest(ContractModel):
    task: str
    messages: list[CimMessage]
    evidence: list[CimEvidence] = Field(default_factory=list)
    context_profile: str
    target_budget_tokens: int = Field(gt=0)
    required_fields: list[str] = Field(default_factory=list)


class RequiredFact(ContractModel):
    field: str
    value: str
    source_id: str


class CimResponse(ContractModel):
    intent: str
    required_facts: list[RequiredFact] = Field(default_factory=list)
    selected_evidence_ids: list[str] = Field(default_factory=list)
    omitted_evidence_ids: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    upstream_messages: list[CimMessage]
    self_reported_complete: bool = Field(
        description="The CIM's own claim. Never treated as verified; the Context Guard checks independently."
    )
