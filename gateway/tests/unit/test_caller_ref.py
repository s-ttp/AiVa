"""`Caller.actor_ref`: the only identifier audit rows and authorship fields record for a caller."""

import pytest

from aiva_gateway.auth.principal import Caller, CallerReferenceError


def test_a_user_is_recorded_by_pseudonym() -> None:
    caller = Caller(kind="user", tenant_id="gct", user_id="u-tester", pseudonym="usr_0123")
    assert caller.actor_ref == "usr_0123"


def test_an_application_is_recorded_as_a_service_principal() -> None:
    caller = Caller(kind="application", tenant_id="gct", application_id="gct-support-chatbot")
    assert caller.actor_ref == "service:gct-support-chatbot"  # never ambiguous with a pseudonym


@pytest.mark.parametrize(
    "caller",
    [
        Caller(kind="user", tenant_id="gct", user_id="u-tester"),  # never falls back to the raw user ID
        Caller(kind="application", tenant_id="gct"),
    ],
)
def test_a_caller_without_a_non_personal_reference_is_refused(caller: Caller) -> None:
    with pytest.raises(CallerReferenceError):
        _ = caller.actor_ref
