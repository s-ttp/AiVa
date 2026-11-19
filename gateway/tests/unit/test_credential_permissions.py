"""ADR-0027: administrators manage application keys; auditors and owners may read them; nobody else."""

from aiva_gateway.auth.principal import Caller
from aiva_gateway.auth.rbac import GRANTS, Permission, granted
from aiva_gateway.core.enums import Role
from aiva_gateway.govern.credentials import may_read

ADMINS = {Role.PLATFORM_ADMIN, Role.TENANT_ADMIN}


def _user(*roles: Role, owns: tuple[str, ...] = ()) -> Caller:
    return Caller(
        kind="user",
        tenant_id="gct",
        pseudonym="p",
        roles=frozenset(roles),
        owned_application_ids=frozenset(owns),
    )


def test_only_administrators_manage_keys() -> None:
    assert GRANTS[Permission.CREDENTIALS_MANAGE] == ADMINS
    for role in Role:
        assert granted(_user(role), Permission.CREDENTIALS_MANAGE) is (role in ADMINS), role
    assert not granted(Caller(kind="application", tenant_id="gct"), Permission.CREDENTIALS_MANAGE)


def test_keys_are_read_by_administrators_auditors_and_an_owner_for_its_own() -> None:
    assert GRANTS[Permission.CREDENTIALS_READ] == ADMINS | {Role.SECURITY_AUDITOR, Role.APPLICATION_OWNER}
    for role in (*ADMINS, Role.SECURITY_AUDITOR):
        assert may_read(_user(role), "gct-support-chatbot"), role
    owner = _user(Role.APPLICATION_OWNER, owns=("gct-support-chatbot",))
    assert may_read(owner, "gct-support-chatbot") and not may_read(owner, "gct-doc-copilot")
    for role in (Role.TESTER, Role.PUBLIC_VISITOR, Role.FINANCE_ANALYST, Role.DEPARTMENT_MANAGER):
        assert not may_read(_user(role), "gct-support-chatbot"), role
    assert not may_read(Caller(kind="application", tenant_id="gct", application_id="x"), "x")
