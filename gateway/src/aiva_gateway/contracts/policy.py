"""AI-PEL policy contracts: OPA input and decision (ADR-0005), evaluate API and admin policy API."""

from typing import Any, Literal

from pydantic import Field, model_validator

from aiva_gateway.catalog.models import PolicyDocument
from aiva_gateway.contracts.common import ContractModel
from aiva_gateway.contracts.messages import ChatMessage, EvidenceList, TestProfile
from aiva_gateway.core.enums import (
    AttemptStage,
    Classification,
    ExecutionPath,
    IdentityMode,
    PolicyAction,
    PolicyStage,
    SourceMode,
)


class PolicyIdentity(ContractModel):
    kind: Literal["application", "user"]
    application_id: str
    identity_mode: IdentityMode
    roles: list[str] = Field(default_factory=list)
    department_id: str | None = None


class PolicySource(ContractModel):
    ip: str | None = Field(description="Client IP from trusted ingress, or the simulated IP for testers.")
    mode: SourceMode


class PolicyRequestFacts(ContractModel):
    task: str | None = None
    scenario_id: str | None = None
    tags: list[str] = Field(default_factory=list, description="Caller hints. Never authorization.")
    quality_tier: str = "standard"
    routing_mode: str = "hybrid"
    preferred_model: str | None = None
    max_output_tokens: int = 0


class PolicyClassification(ContractModel):
    declared: Classification | None = None
    floor: Classification | None = None
    dlp: Classification | None = None
    rlcd: Classification | None = None
    effective: Classification


class PolicyDlpFacts(ContractModel):
    finding_types: list[str] = Field(default_factory=list)
    injection_detected: bool = False
    injection_indicators: list[str] = Field(default_factory=list)
    evidence_injection_detected: bool = Field(
        default=False,
        description="Injection indicators in evidence. Evidence is data, never instructions; this fact "
        "only restricts (e.g. blocks tool side effects).",
    )


class PolicyEgress(ContractModel):
    model_id: str
    stage: AttemptStage


class PolicyQuality(ContractModel):
    """Facts for an answer-quality comparison call (ADR-0023). Set only by the quality-check endpoint."""

    requested: bool = Field(description="A signed-in presenter explicitly asked for this comparison.")
    synthetic_sample: bool = Field(
        description="The run's messages and evidence are exactly one of its scenario's committed synthetic "
        "samples: the sample question or a committed alternate, over the unchanged sample evidence."
    )
    data_designation: str = Field(description="The deployment's data designation, e.g. `synthetic`.")


class PolicyTool(ContractModel):
    """The tool an agent is about to call (`tool` stage). Access and egress come from the registry."""

    tool_id: str
    arguments_sha256: str | None = Field(
        default=None,
        pattern="^[0-9a-f]{64}$",
        description="SHA-256 of the call's canonical JSON arguments. Policies do not match on it; a tool "
        "approval is bound to it, so only the recorded call can be executed with that approval.",
    )


class ApprovalGrant(ContractModel):
    rule_id: str
    tool_id: str | None = Field(
        default=None, description="Set for tool approvals: the grant applies only to calls of this tool."
    )


class PolicyApproval(ContractModel):
    """An approval the gateway verified (status, binding, expiry) before the run started."""

    approval_id: str
    grants: list[ApprovalGrant] = Field(default_factory=list)


class PolicyInput(ContractModel):
    """Input document sent to OPA at `data.aiva.decision`."""

    stage: PolicyStage
    environment: str
    tenant_id: str
    identity: PolicyIdentity
    source: PolicySource
    request: PolicyRequestFacts
    classification: PolicyClassification
    dlp: PolicyDlpFacts
    egress: PolicyEgress | None = None
    tool: PolicyTool | None = None
    quality: PolicyQuality | None = None
    approvals: list[PolicyApproval] = Field(default_factory=list)


class DecisionReason(ContractModel):
    rule_id: str
    message: str
    remediation: str | None = None


class DeniedCandidate(ContractModel):
    model_id: str
    reason: str


class EffectiveConstraints(ContractModel):
    execution_path: ExecutionPath | None = Field(
        default=None, description="Mandatory path, or null when RLCD may choose within the allowed set."
    )
    allowed_models: list[str] = Field(default_factory=list)
    cim: Literal["bypass", "allowed"] = "allowed"
    external_providers: Literal["deny", "allow"] = "allow"
    output_dlp: Literal["required", "optional"] = "optional"
    audit_level: Literal["standard", "enhanced"] = "standard"


class PolicyDecision(ContractModel):
    """Decision returned by OPA, plus the bundle identity the gateway attaches."""

    action: PolicyAction
    reasons: list[DecisionReason] = Field(default_factory=list)
    matched_rule_ids: list[str] = Field(default_factory=list)
    matched_network_id: str | None = None
    effective: EffectiveConstraints = Field(default_factory=EffectiveConstraints)
    denied_candidates: list[DeniedCandidate] = Field(default_factory=list)
    satisfied_approval_rule_ids: list[str] = Field(
        default_factory=list, description="Approval rules satisfied by a verified approval."
    )
    policy_version: str = ""
    bundle_hash: str = ""


class PolicyEvaluateRequest(ContractModel):
    """Dry-run the request-stage policy for a prospective chat request without executing it."""

    messages: list[ChatMessage] = Field(min_length=1, max_length=500)
    evidence: EvidenceList = Field(default_factory=list, max_length=200)
    data_classification: Classification | None = None
    task: str | None = None
    scenario_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    test_profile: TestProfile | None = None


class PolicyEvaluateResponse(ContractModel):
    decision: PolicyDecision
    classification: PolicyClassification
    dlp: PolicyDlpFacts


class PolicyVersionSummary(ContractModel):
    version: int
    status: Literal["draft", "published", "archived"]
    bundle_hash: str
    created_at: str
    created_by: str | None = None
    published_at: str | None = None
    published_by: str | None = None
    notes: str | None = None


class PolicyChange(ContractModel):
    """A difference between a version and the published version (§22.6 review before publish)."""

    target: Literal["policy", "application", "model", "network", "tool", "settings"]
    target_id: str
    change: Literal["added", "removed", "modified"]
    high_impact: bool = Field(description="True when the change relaxes a constraint or blocks broadly.")
    summary: list[str] = Field(default_factory=list)


class PolicyVersionDetail(PolicyVersionSummary):
    policies: list[PolicyDocument]
    published_version: int | None = Field(default=None, description="Version the changes compare against.")
    changes_from_published: list[PolicyChange] = Field(default_factory=list)
    authored_by_viewer: bool = Field(
        default=False,
        description="The caller created this draft. A high-impact draft must be published by someone else.",
    )


class PolicyDraftRequest(ContractModel):
    policies: list[PolicyDocument] = Field(max_length=500)
    notes: str | None = Field(default=None, max_length=2000)


class PolicySample(ContractModel):
    """A prospective request. The gateway runs DLP and classification on it like a real request."""

    __test__ = False  # not a pytest test class

    application_id: str
    source_ip: str | None = Field(default=None, description="Ingress-observed or simulated client IP.")
    source_mode: Literal["observed", "simulated"] = "simulated"
    identity_mode: Literal["authenticated", "simulated"] = "authenticated"
    roles: list[str] = Field(default_factory=list)
    messages: list[ChatMessage] = Field(min_length=1, max_length=50)
    evidence: EvidenceList = Field(default_factory=list, max_length=50)
    data_classification: Classification | None = None
    task: str | None = None
    scenario_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    routing_mode: Literal["hybrid", "local-only", "pinned"] = "hybrid"
    stage: PolicyStage = PolicyStage.REQUEST
    egress: PolicyEgress | None = None
    tool: PolicyTool | None = None
    approvals: list[PolicyApproval] = Field(default_factory=list)


class PolicyTestCase(ContractModel):
    """Either a full policy `input` or a `sample` request the gateway turns into one."""

    __test__ = False  # not a pytest test class

    name: str
    input: PolicyInput | None = None
    sample: PolicySample | None = None
    expected_action: PolicyAction | None = None

    @model_validator(mode="after")
    def _one_source(self) -> "PolicyTestCase":
        if (self.input is None) == (self.sample is None):
            raise ValueError("provide exactly one of 'input' or 'sample'")
        return self


class PolicyTestRequest(ContractModel):
    version: int | None = Field(default=None, description="Version to simulate; defaults to published.")
    cases: list[PolicyTestCase] = Field(min_length=1, max_length=100)


class PolicyTestResult(ContractModel):
    name: str
    input: PolicyInput = Field(description="The evaluated input, including computed classification.")
    decision: PolicyDecision
    expected_action: PolicyAction | None = None
    passed: bool | None = None


class PolicyTestResponse(ContractModel):
    version: int
    status: Literal["draft", "published", "archived"]
    bundle_hash: str
    results: list[PolicyTestResult]
    passed: int = 0
    failed: int = 0


class PolicyPublishRequest(ContractModel):
    version: int = Field(description="A draft to publish, or an archived version to roll back to.")
    approval_note: str = Field(
        min_length=3, max_length=2000, description="Required justification for high-impact changes."
    )


class PolicyPublishResponse(ContractModel):
    version: int
    bundle_hash: str
    published_at: str
    kind: Literal["publish", "rollback"] = "publish"
    previous_version: int | None = None
    previous_bundle_hash: str | None = None
    changes: list[PolicyChange] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


# --- Model switches (ADR-0027 §2) --------------------------------------------------------------


class ModelSwitchRequest(ContractModel):
    """Turn one granted model off, or back on, for one application, as a new policy draft."""

    application_id: str = Field(max_length=64)
    model_id: str = Field(max_length=64)
    enabled: bool = Field(description="False switches the model off for the application; true back on.")
    notes: str | None = Field(default=None, max_length=2000, description="Why; kept on the draft.")


class ModelSwitchCheck(ContractModel):
    """What the application's requests at one classification may use, before and after the draft."""

    classification: Classification
    sovereign_path: bool = Field(description="This classification takes the sovereign direct path.")
    allowed_models_before: list[str] = Field(description="Under the published version.")
    allowed_models_after: list[str] = Field(description="Under the draft.")
    blocked: bool = Field(
        description="No model is left: such requests are blocked by AI policy (AIPEL-ROUTE-001), never "
        "rerouted."
    )
    blocked_before: bool = Field(
        description="Such requests were already blocked under the published version."
    )


class ModelSwitchResponse(ContractModel):
    draft: PolicyVersionDetail = Field(description="The new draft. Test and publish it like any other.")
    base_version: int = Field(description="The published version the draft was built from.")
    application_id: str
    model_id: str
    enabled: bool
    switched_off_models: list[str] = Field(description="The application's models switched off in the draft.")
    high_impact: bool = Field(
        description="Switching a model back on widens access: a different administrator must publish it."
    )
    checks: list[ModelSwitchCheck] = Field(
        description="One per classification the application's requests can have (its floor and above)."
    )
    sovereign_blocked: bool = Field(
        description="The draft leaves the application's confidential, restricted and sovereign-only requests "
        "no model: they would be blocked by AI policy, never rerouted."
    )
    sovereign_blocked_before: bool = Field(
        description="The published version already left those requests no model (e.g. no sovereign grant)."
    )
    warnings: list[str] = Field(default_factory=list, description="Messages to show before publishing.")
