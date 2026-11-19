"""Row-level reporting scope (LLD §21.8, §22.6). Enforced in every query, never only in the UI.

- Platform and tenant administrators, finance analysts and security auditors see their whole tenant, and
  so do the public demo's visitor (ADR-0019) and the presenter signed in as the tester (ADR-0021), without
  the user dimension (no user filter, grouping or principal; no export or identity resolution either).
- A department manager sees the department's runs (every application in it).
- An application owner sees the runs of the applications they own.
Roles combine: the scope is the union. Users are always pseudonymous; resolving a pseudonym to an identity
is a separate, audited permission (`reports:identity`). A filter naming a department or application outside
the scope is refused rather than silently emptied, so a direct API request cannot probe other departments.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import false, or_, true
from sqlalchemy.sql.elements import ColumnElement

from aiva_gateway.auth.principal import Caller, CallerReferenceError
from aiva_gateway.catalog import Catalog
from aiva_gateway.contracts.reports import ReportFilters
from aiva_gateway.contracts.reports import ReportScope as ReportScopeOut
from aiva_gateway.core.enums import Role
from aiva_gateway.core.errors import PermissionDeniedError

TENANT_WIDE = (
    Role.PLATFORM_ADMIN,
    Role.TENANT_ADMIN,
    Role.FINANCE_ANALYST,
    Role.SECURITY_AUDITOR,
    Role.PUBLIC_VISITOR,
    Role.TESTER,
)
# The reporting roles that see the user dimension. The visitor and the tester do not; the visitor hides it
# even combined with another role.
USER_DIMENSION = (
    Role.PLATFORM_ADMIN,
    Role.TENANT_ADMIN,
    Role.FINANCE_ANALYST,
    Role.SECURITY_AUDITOR,
    Role.DEPARTMENT_MANAGER,
    Role.APPLICATION_OWNER,
)

_OUT_OF_SCOPE = "This report filter is outside your reporting scope."
_REMEDIATION = "Remove the filter, or ask an administrator for a role that covers it."


# What a scope without the user dimension sees in place of a run's principal.
HIDDEN_PRINCIPAL = "hidden"


@dataclass(frozen=True)
class ReportScope:
    tenant_id: str
    tenant_wide: bool
    department_id: str | None
    application_ids: frozenset[str]
    # False for the public demo's visitor and the tester: no user filter, grouping or principal column.
    users_visible: bool = True

    def clause(self, table: Any) -> ColumnElement[bool]:
        """SQL predicate for a table with tenant_id, department_id and application_id columns."""
        tenant: ColumnElement[bool] = table.tenant_id == self.tenant_id
        if self.tenant_wide:
            return tenant & true()
        parts: list[ColumnElement[bool]] = []
        if self.department_id:
            parts.append(table.department_id == self.department_id)
        if self.application_ids:
            parts.append(table.application_id.in_(sorted(self.application_ids)))
        return tenant & (or_(*parts) if parts else false())

    def allows(self, *, tenant_id: str, department_id: str | None, application_id: str) -> bool:
        if tenant_id != self.tenant_id:
            return False
        if self.tenant_wide:
            return True
        if self.department_id and department_id == self.department_id:
            return True
        return application_id in self.application_ids

    def covers(self, other: "ReportScope") -> bool:
        """Whether this scope still sees every row `other` saw (an export's snapshot, at download)."""
        if other.tenant_id != self.tenant_id:
            return False
        if self.tenant_wide:
            return True
        if other.tenant_wide:
            return False
        if other.department_id is not None and other.department_id != self.department_id:
            return False
        return other.application_ids <= self.application_ids

    def contract(self) -> ReportScopeOut:
        users = self.users_visible
        if self.tenant_wide:
            return ReportScopeOut(level="tenant", users_visible=users)
        if self.department_id:
            return ReportScopeOut(
                level="department",
                department_id=self.department_id,
                application_ids=sorted(self.application_ids),
                users_visible=users,
            )
        return ReportScopeOut(
            level="applications", application_ids=sorted(self.application_ids), users_visible=users
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "tenant_wide": self.tenant_wide,
            "department_id": self.department_id,
            "application_ids": sorted(self.application_ids),
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> "ReportScope":
        return cls(
            tenant_id=str(data["tenant_id"]),
            tenant_wide=bool(data["tenant_wide"]),
            department_id=data.get("department_id"),
            application_ids=frozenset(data.get("application_ids", [])),
        )


def report_scope(caller: Caller, catalog: Catalog, tenant_id: str | None = None) -> ReportScope:
    """The scope a caller with `reports:read` may see. A different tenant is refused outright."""
    if tenant_id is not None and tenant_id != caller.tenant_id:
        raise PermissionDeniedError(
            "Reports are limited to your own tenant.", remediation="Omit tenant_id or use your own tenant."
        )
    tenant_wide = caller.has_any(*TENANT_WIDE)
    department = caller.department_id if caller.has_any(Role.DEPARTMENT_MANAGER) else None
    applications: set[str] = set()
    if caller.has_any(Role.APPLICATION_OWNER):
        applications |= set(caller.owned_application_ids)
    if department:
        applications |= {a.id for a in catalog.tenant.applications if a.department_id == department}
    return ReportScope(
        tenant_id=caller.tenant_id,
        tenant_wide=tenant_wide,
        department_id=department,
        application_ids=frozenset(applications),
        users_visible=caller.has_any(*USER_DIMENSION) and not caller.has_any(Role.PUBLIC_VISITOR),
    )


def pseudonym_of(caller: Caller) -> str:
    """How Insights records a caller (export owner, audit actor): `Caller.actor_ref`, i.e. its pseudonym or
    an application's `service:` principal. Never a raw user ID (ADR-0015): a caller without a reference is
    refused (403) before anything is written."""
    try:
        return caller.actor_ref
    except CallerReferenceError as exc:
        raise PermissionDeniedError(
            "Reports need a pseudonymous caller.",
            remediation="Sign in again as a user with a reporting role.",
        ) from exc


_NO_USERS = "Reports are shown to your role without the user dimension."
_NO_USERS_REMEDIATION = "Remove the user filter or grouping."


def check_filters(scope: ReportScope, filters: ReportFilters, catalog: Catalog) -> None:
    """Refuse department or application filters outside the caller's scope (LLD §22.10), and a user filter
    from a scope without the user dimension."""
    if filters.user is not None and not scope.users_visible:
        raise PermissionDeniedError(_NO_USERS, remediation=_NO_USERS_REMEDIATION)
    if scope.tenant_wide:
        return
    if filters.department_id is not None:
        owned_departments = {catalog.department_of(a) for a in scope.application_ids}
        if filters.department_id != scope.department_id and filters.department_id not in owned_departments:
            raise PermissionDeniedError(_OUT_OF_SCOPE, remediation=_REMEDIATION)
    if filters.application_id is not None:
        department = catalog.department_of(filters.application_id)
        if not scope.allows(
            tenant_id=scope.tenant_id, department_id=department, application_id=filters.application_id
        ):
            raise PermissionDeniedError(_OUT_OF_SCOPE, remediation=_REMEDIATION)


def check_group(scope: ReportScope, group_by: str) -> None:
    """Refuse grouping by user from a scope without the user dimension."""
    if group_by == "user" and not scope.users_visible:
        raise PermissionDeniedError(_NO_USERS, remediation=_NO_USERS_REMEDIATION)


def shown_principal(scope: ReportScope, principal: str) -> str:
    return principal if scope.users_visible else HIDDEN_PRINCIPAL
