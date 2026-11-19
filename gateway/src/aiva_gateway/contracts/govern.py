"""AiVa Govern contracts: Approval Queue and administrative read models (LLD §22.5 Govern views)."""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from aiva_gateway.catalog.models import PolicyMatch
from aiva_gateway.contracts.common import ContractModel
from aiva_gateway.contracts.policy import ApprovalGrant, DecisionReason, PolicyDecision
from aiva_gateway.core.enums import Classification

# --- Approval Queue ----------------------------------------------------------------------------

ApprovalStatus = Literal["pending", "approved", "denied", "expired", "consumed"]


class ApprovalRequestOut(ContractModel):
    """An approval request. It carries policy facts only: never prompt or evidence text."""

    approval_id: str
    kind: Literal["policy", "tool", "budget"]
    status: ApprovalStatus
    run_id: str = Field(description="The run that required approval.")
    application_id: str
    tool_id: str | None = None
    arguments_sha256: str | None = Field(
        default=None, description="Tool approvals: the arguments hash of the call the approval is bound to."
    )
    rule_ids: list[str] = Field(description="Approval rules this request newly requires.")
    reasons: list[DecisionReason]
    carried_grants: list[ApprovalGrant] = Field(
        default_factory=list, description="Grants of an earlier approval this run consumed (multi-step runs)."
    )
    effective_classification: Classification | None = None
    task: str | None = None
    scenario_id: str | None = None
    matched_network_id: str | None = None
    identity_mode: str | None = None
    source_mode: str | None = None
    requested_by: str | None = Field(default=None, description="Requester pseudonym, if a user.")
    requested_at: datetime
    expires_at: datetime
    policy_version_at_request: str
    decided_at: datetime | None = None
    decided_by: str | None = Field(default=None, description="Approver pseudonym.")
    decision_note: str | None = None
    approved_grants: list[ApprovalGrant] = Field(
        default_factory=list, description="Everything a resubmission with this approval may rely on."
    )
    policy_version_at_decision: str | None = None
    consumed_by_run_id: str | None = None
    approved_amount_micros: int | None = Field(
        default=None,
        description="Budget approvals: the denied reservation's worst-case estimate (integer micro-USD) the "
        "approval covers. Estimated, not measured.",
    )
    requested_by_viewer: bool = Field(
        default=False, description="The caller of this API made the request (computed per caller)."
    )
    viewer_can_decide: bool = Field(
        default=False,
        description="Approve or deny by the caller would be accepted now: the caller may decide this kind "
        "and application, did not make the request, and the request is pending (computed per caller).",
    )


class ApprovalList(ContractModel):
    approvals: list[ApprovalRequestOut]


class ApprovalDecisionRequest(ContractModel):
    note: str = Field(min_length=3, max_length=2000, description="Why the request is approved or denied.")


class ApprovalDecisionResponse(ContractModel):
    approval: ApprovalRequestOut
    outcome: Literal["approved", "denied", "closed_by_policy"]
    re_evaluation: PolicyDecision | None = Field(
        default=None, description="Decision of the current policy version, evaluated before approving."
    )


# --- Administrative reads ----------------------------------------------------------------------


class TrustedNetworkRef(ContractModel):
    id: str
    name: str
    cidr: str


class CredentialOut(ContractModel):
    """Credential metadata. Secrets are never returned after creation."""

    key_id: str
    label: str
    created_at: datetime
    revoked_at: datetime | None = None
    created_by: str = Field(description="The issuer's pseudonym, or `cli` for a key made in the container.")
    revoked_by: str | None = None
    expires_at: datetime | None = Field(
        default=None, description="Keys issued from the admin console always expire; a CLI key may not."
    )
    last_used_at: datetime | None = Field(default=None, description="Accurate to the hour.")
    active: bool = Field(description="Neither revoked nor expired: the key authenticates.")


class CredentialCreate(ContractModel):
    """Issue a key for an application (ADR-0027)."""

    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] = Field(
        description="What the key is for. Shown in the key list."
    )
    expires_in_days: int = Field(default=30, ge=1, le=90, description="30 by default; at most 90.")


class CredentialIssued(ContractModel):
    """The new key. `credential` is the only time its secret is returned: copy it now."""

    application_id: str
    credential: str = Field(description="`aiva_<key_id>.<secret>`. Shown once; AiVa keeps only an HMAC.")
    metadata: CredentialOut


class CredentialList(ContractModel):
    application_id: str
    active_limit: int = Field(description="Active keys an application may hold at once.")
    viewer_can_issue: bool = Field(
        description="Issuing a key by the caller would be accepted now: the caller may manage keys and the "
        "application is below its active limit (computed per caller)."
    )
    viewer_can_revoke: bool = Field(description="The caller may revoke this application's keys.")
    credentials: list[CredentialOut]


class ApplicationOut(ContractModel):
    id: str
    name: str
    department_id: str
    enabled: bool
    classification_floor: Classification
    allowed_tasks: list[str]
    allowed_models: list[str]
    allowed_tools: list[str]
    trusted_networks: list[TrustedNetworkRef]
    internet_callers: bool = Field(
        default=False,
        description="Accepts calls made with one of its keys from any source (ADR-0027). Users still need a "
        "trusted network.",
    )
    switched_off_models: list[str] = Field(
        default_factory=list,
        description="Granted models the published version switches off for this application (ADR-0027).",
    )
    credentials: list[CredentialOut] = Field(default_factory=list)


class ApplicationList(ContractModel):
    policy_version: str
    bundle_hash: str
    viewer_can_switch: bool = Field(
        default=False,
        description="The caller may draft model switches (`policies:write`), computed per caller.",
    )
    applications: list[ApplicationOut]


class TrustedNetworkOut(TrustedNetworkRef):
    application_ids: list[str]


class IngressConfigOut(ContractModel):
    """How the gateway derives the client IP (ADR-0007, LLD §5)."""

    trusted_proxies: list[str]
    forwarded_for_trusted: bool = Field(
        description="False: X-Forwarded-For is ignored; the TCP peer is used."
    )
    simulated_ingress_allowed: bool = Field(description="Testers may supply a simulated source IP.")


class TrustedNetworkList(ContractModel):
    policy_version: str
    bundle_hash: str
    ingress: IngressConfigOut
    networks: list[TrustedNetworkOut]


class ModelRateCardOut(ContractModel):
    """The pinned price a model is billed at (`config/rate_cards.yaml`), in integer micro-USD.

    A price is configuration, cited by `id`, `version`, `source` and `effective_from`; provenance labels
    belong to amounts (costs), never to prices (ADR-0006). A $0 token price on a GPU-served model is the
    POC convention: its GPU cost is reported separately.
    """

    id: str
    version: str = Field(description="Version of the rate-card file the price belongs to.")
    currency: Literal["USD"] = "USD"
    input_micros_per_mtok: int = Field(description="Micro-USD per 1M input tokens.")
    output_micros_per_mtok: int = Field(description="Micro-USD per 1M output tokens.")
    cached_input_micros_per_mtok: int = Field(description="Micro-USD per 1M cached input tokens.")
    gpu_micros_per_hour: int | None = Field(
        default=None, description="Micro-USD per GPU hour, for models on a priced GPU endpoint."
    )
    effective_from: date
    source: str = Field(description="Where the price comes from, and when it was taken.")


class ModelOut(ContractModel):
    id: str
    display_name: str
    role: Literal["upstream", "cim", "grader"]
    catalog_configured: bool = Field(
        description="False when the published bundle names a model the gateway's catalog does not "
        "configure; catalog fields are then null."
    )
    provider: str | None = None
    provider_model_id: str | None = None
    revision: str | None = None
    region: str | None = None
    context_window: int | None = None
    provider_route: str | None = None
    adapter: str | None = None
    sovereign: bool
    external: bool
    enabled: bool
    application_ids: list[str] = Field(description="Applications granted this model.")
    switched_off_application_ids: list[str] = Field(
        default_factory=list,
        description="Granted applications the published version switches this model off for (ADR-0027).",
    )
    rate_card: ModelRateCardOut | None = None


class PolicyAllowlistOut(ContractModel):
    """A policy that narrows the model set when it matches."""

    policy_id: str
    title: str
    match: PolicyMatch
    allowed_models: list[str] | None = None
    execution_path: str | None = None
    external_providers: str | None = None
    cim: str | None = None


class ModelAllowlist(ContractModel):
    policy_version: str
    bundle_hash: str
    viewer_can_switch: bool = Field(
        default=False,
        description="The caller may draft model switches (`policies:write`), computed per caller.",
    )
    models: list[ModelOut]
    policy_allowlists: list[PolicyAllowlistOut]
    invariants: list[str] = Field(description="Built-in constraints no policy version can remove.")


class ToolOut(ContractModel):
    id: str
    display_name: str
    description: str
    access: Literal["read", "write"]
    egress: Literal["internal", "external"]
    enabled: bool
    application_ids: list[str]


class ToolList(ContractModel):
    policy_version: str
    bundle_hash: str
    tools: list[ToolOut]
