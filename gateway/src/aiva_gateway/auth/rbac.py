"""Role-based permissions (LLD §21.8). Row-level scoping is enforced in each query, never only in the UI."""

from enum import StrEnum

from aiva_gateway.auth.principal import Caller, RunIdentity
from aiva_gateway.core.enums import Role
from aiva_gateway.core.errors import PermissionDeniedError


class Permission(StrEnum):
    CHAT_TEST = "chat:test"
    # Ask for a live run (provider calls) rather than the recorded replay (ADR-0019). A visitor's live run
    # is also limited by FinOps (per-visitor quota, public-demo budget).
    CHAT_LIVE = "chat:live"
    RUNS_READ = "runs:read"
    POLICIES_READ = "policies:read"
    POLICIES_WRITE = "policies:write"
    POLICIES_PUBLISH = "policies:publish"
    BUDGETS_READ = "budgets:read"
    BUDGETS_WRITE = "budgets:write"
    REPORTS_READ = "reports:read"
    REPORTS_EXPORT = "reports:export"
    REPORTS_IDENTITY = "reports:identity"
    EVALUATIONS_RUN = "evaluations:run"
    CATALOG_READ = "catalog:read"
    APPROVALS_READ = "approvals:read"
    APPROVALS_DECIDE = "approvals:decide"
    # Download a record-mode gateway's recorded provider exchanges (ADR-0009): platform administrators only.
    RECORDINGS_EXPORT = "recordings:export"
    # Wake Endpoints A and B, or put them to sleep, from the presenter's live demo page (ADR-0021). Never a
    # public visitor: a visitor's live run never wakes an endpoint (ADR-0019).
    DEMO_WAKE = "demo:wake"
    # Compare a presenter's live answer with the frontier reference, graded by the pinned grader (ADR-0023).
    # Never a public visitor. Policy checks the same roles again before each comparison call.
    DEMO_COMPARE = "demo:compare"
    # Issue and revoke an application's keys from the admin console (ADR-0027), and read their metadata.
    # A key can spend in live mode, so only administrators manage keys; the secret is shown once.
    CREDENTIALS_MANAGE = "credentials:manage"
    CREDENTIALS_READ = "credentials:read"


_ADMINS = {Role.PLATFORM_ADMIN, Role.TENANT_ADMIN}
# The public demo's visitor (ADR-0019): launches console tests and reads its own runs, reports (without
# the user dimension), policies and the catalog. It never decides, writes, exports, resolves identities,
# runs evaluations or administers.
_VISITOR = {Role.PUBLIC_VISITOR}

GRANTS: dict[Permission, set[Role]] = {
    Permission.CHAT_TEST: {Role.TESTER} | _VISITOR,
    Permission.CHAT_LIVE: _ADMINS | {Role.TESTER} | _VISITOR,
    Permission.RUNS_READ: _ADMINS
    | {Role.SECURITY_AUDITOR, Role.DEPARTMENT_MANAGER, Role.APPLICATION_OWNER, Role.TESTER}
    | _VISITOR,
    Permission.POLICIES_READ: _ADMINS | {Role.SECURITY_AUDITOR} | _VISITOR,
    Permission.POLICIES_WRITE: _ADMINS,
    Permission.POLICIES_PUBLISH: _ADMINS,
    Permission.BUDGETS_READ: _ADMINS
    | {Role.FINANCE_ANALYST, Role.DEPARTMENT_MANAGER, Role.APPLICATION_OWNER},
    Permission.BUDGETS_WRITE: _ADMINS | {Role.FINANCE_ANALYST},
    # The tester is the presenter on /live, who also opens the value page (/week). Like the visitor, it sees
    # the tenant without the user dimension, and never exports or resolves identities (ADR-0021 amendment).
    Permission.REPORTS_READ: _ADMINS
    | {Role.FINANCE_ANALYST, Role.DEPARTMENT_MANAGER, Role.SECURITY_AUDITOR, Role.APPLICATION_OWNER}
    | {Role.TESTER}
    | _VISITOR,
    Permission.REPORTS_EXPORT: _ADMINS | {Role.FINANCE_ANALYST, Role.SECURITY_AUDITOR},
    # Resolving a report pseudonym to an identity is separately permissioned and audited (LLD §21.8).
    Permission.REPORTS_IDENTITY: {Role.TENANT_ADMIN, Role.SECURITY_AUDITOR},
    Permission.EVALUATIONS_RUN: _ADMINS | {Role.TESTER},
    Permission.CATALOG_READ: set(Role),
    # Row-level scope (own application or department; finance: budget approvals) is in govern.approvals.
    Permission.APPROVALS_READ: _ADMINS
    | {Role.SECURITY_AUDITOR, Role.APPLICATION_OWNER, Role.DEPARTMENT_MANAGER, Role.FINANCE_ANALYST},
    Permission.APPROVALS_DECIDE: _ADMINS | {Role.APPLICATION_OWNER, Role.FINANCE_ANALYST},
    Permission.RECORDINGS_EXPORT: {Role.PLATFORM_ADMIN},
    Permission.DEMO_WAKE: _ADMINS | {Role.TESTER},
    Permission.DEMO_COMPARE: _ADMINS | {Role.TESTER},
    Permission.CREDENTIALS_MANAGE: _ADMINS,
    # Row-level: an application owner reads its own applications' keys only (govern.credentials).
    Permission.CREDENTIALS_READ: _ADMINS | {Role.SECURITY_AUDITOR, Role.APPLICATION_OWNER},
}


_ROLE_VALUES = frozenset(r.value for r in Role)


def granted(caller: Caller, permission: Permission) -> bool:
    return caller.kind == "user" and caller.has_any(*GRANTS[permission])


def identity_granted(identity: RunIdentity, permission: Permission) -> bool:
    """`granted` for the principal a run executes as (its roles are recorded on the RunIdentity)."""
    roles = {Role(r) for r in identity.roles if r in _ROLE_VALUES}
    return identity.principal_kind == "user" and bool(roles & GRANTS[permission])


def require(caller: Caller, permission: Permission) -> None:
    if not granted(caller, permission):
        raise PermissionDeniedError(
            "You do not have permission for this action.",
            remediation=f"Ask an administrator for a role that grants '{permission.value}'.",
        )
