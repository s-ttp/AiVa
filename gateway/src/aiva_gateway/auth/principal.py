from dataclasses import dataclass, field
from typing import Literal

from aiva_gateway.core.enums import IdentityMode, Role, SourceMode

# An application principal in audit rows and reports: never ambiguous with a user's pseudonym.
SERVICE_PREFIX = "service:"


@dataclass(frozen=True)
class Caller:
    """The authenticated caller of an API request."""

    kind: Literal["application", "user"]
    tenant_id: str
    application_id: str | None = None
    credential_id: str | None = None
    user_id: str | None = None
    pseudonym: str | None = None
    roles: frozenset[Role] = field(default_factory=frozenset)
    department_id: str | None = None
    owned_application_ids: frozenset[str] = field(default_factory=frozenset)

    def has_any(self, *roles: Role) -> bool:
        return bool(self.roles & set(roles))

    @property
    def actor_ref(self) -> str:
        """Who this caller is in audit rows and authorship fields: a user's pseudonym, or
        `service:<application_id>` for an application credential (the convention reports use). Never a raw
        user ID (LLD §17, §21.8); a user without a pseudonym is an authentication bug, so this raises
        instead of falling back."""
        if self.kind == "user":
            if not self.pseudonym:
                raise CallerReferenceError("A user caller has no pseudonym.")
            return self.pseudonym
        if not self.application_id:
            raise CallerReferenceError("An application caller has no application ID.")
        return f"{SERVICE_PREFIX}{self.application_id}"


class CallerReferenceError(RuntimeError):
    """A caller with no non-personal reference. Raised rather than recording a raw identifier."""


@dataclass(frozen=True)
class RunIdentity:
    """Who a run executes as, and from where (simulated only for testers)."""

    application_id: str
    identity_mode: IdentityMode
    source_ip: str | None
    source_mode: SourceMode
    principal_kind: Literal["application", "user"]
    principal_pseudonym: str | None
    roles: tuple[str, ...]
    # The department of the user who started the run (the public demo's visitor: `public-demo`), for
    # attribution; None for an application. The run's budget department stays the application's.
    principal_department_id: str | None = None

    @property
    def is_public_visitor(self) -> bool:
        """A public demo session (ADR-0019): replay by default, and live runs limited by FinOps."""
        return self.principal_kind == "user" and Role.PUBLIC_VISITOR.value in self.roles
