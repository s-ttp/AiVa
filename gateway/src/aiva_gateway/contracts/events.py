"""Common event envelope (LLD §22.9) shared by telemetry, reporting and the UI."""

from datetime import datetime

from pydantic import Field

from aiva_gateway.contracts.common import ContractModel
from aiva_gateway.core.enums import Classification, ExecutionPath, PolicyAction, Provenance


class EventTokenUsage(ContractModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cim_input_tokens: int = 0
    cim_output_tokens: int = 0


class EventCost(ContractModel):
    api_micros: int = 0
    gpu_estimated_micros: int = 0
    avoided_micros: int = 0
    currency: str = "USD"
    rate_card_version: str


class EventEnvelope(ContractModel):
    event_type: str = Field(description="e.g. 'run.completed', 'policy.denied', 'budget.threshold'.")
    occurred_at: datetime
    trace_id: str
    run_id: str
    tenant_id: str
    application_id: str
    user_pseudonym: str | None = None
    task_category: str | None = None
    classification_confidence: float | None = None
    classifier_version: str | None = None
    taxonomy_version: str | None = None
    policy_decision: PolicyAction
    policy_version: str
    sensitivity: Classification
    execution_path: ExecutionPath
    model_id: str | None = None
    token_usage: EventTokenUsage
    cost: EventCost
    provenance: Provenance
    data_designation: str
