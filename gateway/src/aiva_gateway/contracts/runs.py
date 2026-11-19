"""Run and trace read models (`GET /v1/runs/{id}`, `GET /v1/runs/{id}/trace`)."""

from datetime import datetime
from typing import Any

from aiva_gateway.contracts.common import ContractModel
from aiva_gateway.core.enums import CimState, ExecutionPath, Outcome, RunStatus


class TraceSpanOut(ContractModel):
    span_id: str
    parent_span_id: str | None
    name: str
    status: str
    started_at: datetime
    duration_ms: float
    attributes: dict[str, Any]


class RunTrace(ContractModel):
    run_id: str
    trace_id: str
    spans: list[TraceSpanOut]


class ModelAttemptOut(ContractModel):
    attempt_id: str
    stage: str
    model_id: str
    provider: str
    status: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    latency_ms: float | None
    provenance: str


class PolicyEventOut(ContractModel):
    stage: str
    tool_id: str | None = None
    action: str
    policy_version: str
    bundle_hash: str
    matched_rule_ids: list[str]
    effective_classification: str


class RunDetail(ContractModel):
    run_id: str
    trace_id: str
    tenant_id: str
    application_id: str
    scenario_id: str | None
    status: RunStatus
    outcome: Outcome
    execution_path: ExecutionPath
    selected_model: str | None
    cim_state: CimState
    cache_hit: bool
    identity_mode: str
    source_mode: str
    policy_version: str | None
    data_designation: str
    created_at: datetime
    completed_at: datetime | None
    latency_ms: float | None
    attempts: list[ModelAttemptOut]
    policy_events: list[PolicyEventOut]
