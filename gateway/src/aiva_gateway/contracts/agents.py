"""Agentic workflow contracts (LLD §15 #6: multi-step service incident, per-tool policy, cumulative budgets).

A workflow is a sequence of steps. Each model step is one governed chat run (policy, reservation, egress
checks and metering as for any run) that shares the workflow's `workflow_id`, so every step draws on one
FinOps `workflow` budget envelope (LLD §12). Every tool call is authorized by the `tool` policy stage of
the model step that precedes it, and runs only on `allow` (ADR-0012). Tool output is passed to later steps
as evidence: untrusted data, never instructions (LLD §17). The agent never takes punitive action on its own:
such actions are recorded for human review.

Tool output and model text appear only in the `POST` response; stored workflows keep hashes, never text.
"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from aiva_gateway.contracts.chat import RoutingOptions
from aiva_gateway.contracts.common import ContractModel, ErrorBody, PageInfo
from aiva_gateway.contracts.messages import TestProfile
from aiva_gateway.core.enums import (
    CimState,
    Classification,
    ExecutionPath,
    PolicyAction,
    Provenance,
    RunStatus,
)

AgentId = Literal["service-incident"]
WorkflowStatus = Literal[
    "completed", "completed_with_denials", "approval_required", "blocked", "budget_exhausted", "failed"
]
StepStatus = Literal["completed", "denied", "approval_required", "human_review", "skipped", "failed"]


class AgentWorkflowRequest(ContractModel):
    agent: AgentId = "service-incident"
    incident_id: str = Field(pattern=r"^INC-[0-9]{3,8}$", description="Synthetic ITSM incident ID.")
    instructions: str | None = Field(
        default=None,
        max_length=4000,
        description="Operator note for the first step. Data for the model; it never authorizes a tool.",
    )
    data_classification: Classification | None = Field(
        default=None,
        description="Declared classification. The most restrictive of declared and detected wins.",
    )
    quality_tier: Literal["standard", "high", "critical"] = "standard"
    routing: RoutingOptions = Field(
        default_factory=RoutingOptions,
        description="Routing hints for every model step (never authorization).",
    )
    max_output_tokens: int = Field(
        default=1024,
        ge=64,
        le=4096,
        description=(
            "Per model step. The default matches the Control Center's: Gemini Flash-Lite's analysis step "
            "ran to 638 tokens live, past the earlier 600 (ADR-0009, 2026-10-22)."
        ),
    )
    scenario_id: str | None = None
    test_profile: TestProfile | None = None


class ModelStepOut(ContractModel):
    run_id: str
    trace_id: str
    status: RunStatus
    selected_model: str | None
    execution_path: ExecutionPath
    cim_state: CimState
    effective_classification: Classification | None
    cost_micros: int = Field(description="Provider-billable API cost of the step (excludes GPU estimates).")
    provenance: Provenance
    response: str | None = Field(default=None, description="Model text; only in the POST response.")
    error: ErrorBody | None = None


class ToolCallOut(ContractModel):
    tool_id: str
    access: Literal["read", "write"] | None = Field(
        description="From the tool registry; None if unregistered."
    )
    egress: Literal["internal", "external"] | None
    arguments: dict[str, str] = Field(default_factory=dict, description="Synthetic identifiers only.")
    policy_action: PolicyAction
    rule_ids: list[str] = Field(default_factory=list)
    approval_id: str | None = Field(default=None, description="Set when the tool call needs approval.")
    executed: bool
    evidence_id: str | None = Field(default=None, description="Evidence ID later steps cite for the output.")
    output_sha256: str | None = None
    output: str | None = Field(default=None, description="Tool output; only in the POST response.")


class AgentStepOut(ContractModel):
    index: int
    kind: Literal["model", "tool"]
    name: str = Field(description="Step name, e.g. 'plan', 'analyse', or the tool ID.")
    status: StepStatus
    message: str = Field(description="UI copy, e.g. 'Allowed by policy', 'Approval required'.")
    authorized_by_run_id: str | None = Field(
        default=None, description="Tool steps: the run whose `tool` policy stage decided this call."
    )
    model: ModelStepOut | None = None
    tool: ToolCallOut | None = None
    started_at: datetime
    completed_at: datetime


class WorkflowBudgetOut(ContractModel):
    """The FinOps `workflow` budget every step draws on (scope `<application_id>/<workflow_id>`)."""

    scope_id: str
    cap_micros: int | None = Field(description="None when no workflow budget applies.")
    committed_micros: int = Field(description="Settled spend of all steps so far.")
    remaining_micros: int | None
    on_exhaustion: str | None = None
    provenance: Provenance


class AgentWorkflowOut(ContractModel):
    workflow_id: str
    agent: AgentId
    definition_version: str
    tenant_id: str
    application_id: str
    incident_id: str
    status: WorkflowStatus
    steps: list[AgentStepOut]
    pending_approval_ids: list[str] = Field(default_factory=list)
    human_review_tool_ids: list[str] = Field(
        default_factory=list, description="Actions recommended to a human; never executed by the agent."
    )
    report: str | None = Field(default=None, description="Final step's text; only in the POST response.")
    budget: WorkflowBudgetOut
    provenance: Provenance = Field(description="Least trustworthy provenance among the steps.")
    data_designation: Literal["synthetic", "actual"]
    created_at: datetime
    completed_at: datetime | None = None
    error: ErrorBody | None = None


class AgentWorkflowResumeRequest(ContractModel):
    """Continue a workflow paused at `approval_required` once an approver has approved (ADR-0012, ADR-0014).

    The paused step is resubmitted unchanged with the approval: it runs as a new governed run, policy is
    evaluated again (approval never overrides a deny) and a budget approval lifts the cap once. The
    remaining steps of the definition then continue under the same workflow budget.
    """

    approval_id: str = Field(max_length=40, description="An approved request raised by this workflow.")
    test_profile: TestProfile | None = None


class AgentWorkflowList(ContractModel):
    items: list[AgentWorkflowOut]
    page: PageInfo
