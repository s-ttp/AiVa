"""Shared vocabularies. Wire values are the LLD's kebab-case terms."""

from enum import StrEnum


class Classification(StrEnum):
    """Data classification, ordered from least to most restrictive (LLD §7, §21.3)."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"
    SOVEREIGN_ONLY = "sovereign-only"

    @property
    def rank(self) -> int:
        return _CLASSIFICATION_ORDER.index(self)

    @property
    def requires_sovereign_path(self) -> bool:
        return self.rank >= Classification.CONFIDENTIAL.rank


_CLASSIFICATION_ORDER = list(Classification)


def most_restrictive(*levels: Classification | None) -> Classification:
    """Most restrictive classification wins; absent signals are ignored (LLD §7)."""
    present = [level for level in levels if level is not None]
    if not present:
        return Classification.INTERNAL
    return max(present, key=lambda level: level.rank)


class ExecutionPath(StrEnum):
    CACHE_RETURN = "cache-return"
    SOVEREIGN_DIRECT = "sovereign-direct"
    LOCAL_FAST = "local-fast"
    CIM_ASSISTED = "cim-assisted"
    NONE = "none"


class RunStatus(StrEnum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    APPROVAL_REQUIRED = "approval_required"
    FAILED = "failed"


class Outcome(StrEnum):
    """Run outcome taxonomy (LLD §21.3)."""

    SUCCESS = "success"
    QUALITY_FAIL = "quality_fail"
    POLICY_DENIED = "policy_denied"
    QUOTA_DENIED = "quota_denied"
    APPROVAL_REQUIRED = "approval_required"
    TIMEOUT = "timeout"
    ERROR = "error"


class Provenance(StrEnum):
    """Where a number or output came from (ADR-0006)."""

    MEASURED = "measured"
    ESTIMATED = "estimated"
    SIMULATED = "simulated"
    REPLAYED = "replayed"
    SYNTHETIC = "synthetic"


class CimState(StrEnum):
    INVOKED = "invoked"
    BYPASSED_POLICY = "bypassed-policy"
    SKIPPED_SUFFICIENT = "skipped-sufficient"
    SKIPPED_CACHE = "skipped-cache"
    NOT_REACHED = "not-reached"


class IdentityMode(StrEnum):
    AUTHENTICATED = "authenticated"
    SIMULATED = "simulated"


class SourceMode(StrEnum):
    OBSERVED = "observed"
    SIMULATED = "simulated"


class PolicyAction(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVAL_REQUIRED = "approval_required"


class PolicyStage(StrEnum):
    REQUEST = "request"
    EGRESS = "egress"
    TOOL = "tool"


class AttemptStage(StrEnum):
    CIM = "cim"
    UPSTREAM = "upstream"
    VALIDATION = "validation"
    BASELINE = "baseline"
    # A presenter's answer-quality comparison of a live answer (ADR-0023): the frontier reference's answer
    # and the grader's verdict. Each is an egress call, checked by policy before it is made.
    QUALITY_REFERENCE = "quality_reference"
    QUALITY_GRADING = "quality_grading"


class ChargeType(StrEnum):
    UPSTREAM_INFERENCE = "upstream_inference"
    CIM = "cim"
    RETRIEVAL = "retrieval"
    GPU_ESTIMATE = "gpu_estimate"
    RETRY = "retry"
    BASELINE_COMPARISON = "baseline_comparison"
    ADJUSTMENT = "adjustment"
    TOOL_CALL = "tool_call"  # a tool call a provider bills (e.g. web search), priced by `tool_rate_cards`
    # A presenter's answer-quality comparison (ADR-0023): measurement cost on the run, never savings.
    QUALITY_REFERENCE = "quality_reference"
    QUALITY_GRADING = "quality_grading"


class Role(StrEnum):
    """Reporting and administration roles (LLD §21.8), the Test Console tester and the public demo's
    visitor (ADR-0019)."""

    PLATFORM_ADMIN = "platform_admin"
    TENANT_ADMIN = "tenant_admin"
    FINANCE_ANALYST = "finance_analyst"
    DEPARTMENT_MANAGER = "department_manager"
    SECURITY_AUDITOR = "security_auditor"
    APPLICATION_OWNER = "application_owner"
    TESTER = "tester"
    PUBLIC_VISITOR = "public_visitor"
