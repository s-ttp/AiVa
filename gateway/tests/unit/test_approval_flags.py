"""Per-caller flags on approval requests mirror the checks `decide` enforces (ADR-0012)."""

from datetime import UTC, datetime, timedelta

import pytest

from aiva_gateway.auth.principal import Caller
from aiva_gateway.core.enums import Role
from aiva_gateway.db.models import ApprovalRequestRow
from aiva_gateway.govern.approvals import to_out

APP = "gct-support-chatbot"


def _row(
    *, kind: str = "policy", status: str = "pending", requester: str | None = "usr_requester"
) -> ApprovalRequestRow:
    now = datetime.now(UTC)
    return ApprovalRequestRow(
        id="apr_1",
        run_id="run_1",
        tenant_id="gct",
        application_id=APP,
        kind=kind,
        rule_ids=["TEST"],
        reasons=[],
        status=status,
        request_hash="h",
        principal_kind="user" if requester else "application",
        principal_pseudonym=requester,
        policy_input={},
        policy_version_at_request="1",
        bundle_hash_at_request="b",
        requested_at=now,
        expires_at=now + timedelta(hours=1),
        approved_grants=[],
    )


def _user(*roles: Role, pseudonym: str = "usr_viewer", owns: frozenset[str] = frozenset()) -> Caller:
    return Caller(
        kind="user", tenant_id="gct", pseudonym=pseudonym, roles=frozenset(roles), owned_application_ids=owns
    )


OWNER = _user(Role.APPLICATION_OWNER, owns=frozenset({APP}))


@pytest.mark.parametrize(
    ("viewer", "row", "requested", "can_decide"),
    [
        (OWNER, _row(), False, True),
        (_user(Role.TENANT_ADMIN), _row(), False, True),
        # The requester never decides their own request, whatever their roles.
        (
            _user(Role.TESTER, Role.APPLICATION_OWNER, pseudonym="usr_requester", owns=frozenset({APP})),
            _row(),
            True,
            False,
        ),
        # An application owner of another application cannot decide.
        (_user(Role.APPLICATION_OWNER, owns=frozenset({"gct-noc-assistant"})), _row(), False, False),
        (_user(Role.SECURITY_AUDITOR), _row(), False, False),
        (_user(Role.FINANCE_ANALYST), _row(kind="budget"), False, True),
        (_user(Role.FINANCE_ANALYST), _row(), False, False),
        (OWNER, _row(status="approved"), False, False),  # only pending requests are decidable
        (Caller(kind="application", tenant_id="gct", application_id=APP), _row(requester=None), True, False),
        (_user(Role.TENANT_ADMIN), _row(requester=None), False, True),
    ],
)
def test_viewer_flags(viewer: Caller, row: ApprovalRequestRow, requested: bool, can_decide: bool) -> None:
    out = to_out(row, viewer=viewer)
    assert (out.requested_by_viewer, out.viewer_can_decide) == (requested, can_decide)


def test_expired_requests_are_not_decidable() -> None:
    row = _row()
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    assert to_out(row, viewer=OWNER).viewer_can_decide is False


def test_flags_default_to_false_without_a_viewer() -> None:
    out = to_out(_row())
    assert (out.requested_by_viewer, out.viewer_can_decide) == (False, False)
