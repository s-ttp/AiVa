"""`demo:wake` (ADR-0021): presenters may wake Endpoints A and B or put them to sleep; a visitor never may."""

from aiva_gateway.auth.principal import Caller, RunIdentity
from aiva_gateway.auth.rbac import GRANTS, Permission, granted, identity_granted
from aiva_gateway.core.enums import IdentityMode, Role, SourceMode

PRESENTERS = {Role.PLATFORM_ADMIN, Role.TENANT_ADMIN, Role.TESTER}


def test_only_administrators_and_testers_hold_demo_wake() -> None:
    assert Permission.DEMO_WAKE == "demo:wake"
    assert GRANTS[Permission.DEMO_WAKE] == PRESENTERS
    for role in Role:
        caller = Caller(kind="user", tenant_id="gct", pseudonym="p", roles=frozenset({role}))
        assert granted(caller, Permission.DEMO_WAKE) is (role in PRESENTERS), role


def test_a_public_visitor_never_wakes_an_endpoint() -> None:
    # ADR-0019: a visitor reads node status (`chat:live`) but never changes it.
    visitor = Caller(kind="user", tenant_id="gct", pseudonym="p", roles=frozenset({Role.PUBLIC_VISITOR}))
    assert granted(visitor, Permission.CHAT_LIVE) and not granted(visitor, Permission.DEMO_WAKE)
    run = RunIdentity(
        application_id="gct-support-chatbot",
        identity_mode=IdentityMode.SIMULATED,
        source_ip=None,
        source_mode=SourceMode.SIMULATED,
        principal_kind="user",
        principal_pseudonym="usr_x",
        roles=(Role.PUBLIC_VISITOR.value,),
    )
    assert not identity_granted(run, Permission.DEMO_WAKE)


def test_an_application_credential_never_wakes_an_endpoint() -> None:
    assert not granted(Caller(kind="application", tenant_id="gct"), Permission.DEMO_WAKE)
