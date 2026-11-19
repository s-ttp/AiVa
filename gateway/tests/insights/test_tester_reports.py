"""The presenter, signed in as the tester, reads reports (ADR-0021 amendment, 2026-10-22).

The scope is the public visitor's (ADR-0019): the whole tenant without the user dimension, with no export
and no identity resolution.
"""

from datetime import UTC, datetime, timedelta

import pytest

from aiva_gateway.auth.principal import Caller
from aiva_gateway.auth.rbac import Permission, granted
from aiva_gateway.core.enums import Role
from aiva_gateway.reporting.scope import report_scope
from tests.insights.conftest import refresh_window

TODAY = datetime.now(UTC).date()
WINDOW = {"from": TODAY.isoformat(), "to": TODAY.isoformat()}
DAY_START = datetime.combine(TODAY, datetime.min.time(), tzinfo=UTC)
# What the value page (/week, `story/WeekPage.tsx`) reads.
WEEK_PAGE = [
    ("/v1/reports/overview", {}),
    ("/v1/reports/quality", {"group_by": "application"}),
    ("/v1/reports/consumption", {"group_by": "application", "limit": 100}),
]


@pytest.fixture
async def fresh(traffic, services):
    await refresh_window(services, DAY_START, DAY_START + timedelta(days=1))
    return traffic


@pytest.fixture
def as_user(make_client, user_token):
    return lambda user: make_client(user_token(user))


def _caller(*roles: Role) -> Caller:
    return Caller(kind="user", tenant_id="gct", pseudonym="usr_p", roles=frozenset(roles))


def test_the_tester_reads_reports_but_never_exports_or_resolves_identities() -> None:
    tester = _caller(Role.TESTER)
    assert granted(tester, Permission.REPORTS_READ)
    assert not granted(tester, Permission.REPORTS_EXPORT)
    assert not granted(tester, Permission.REPORTS_IDENTITY)


def test_the_testers_scope_is_the_visitors_and_roles_still_combine(services) -> None:  # type: ignore[no-untyped-def]
    catalog = services.catalog
    for roles in ((Role.TESTER,), (Role.PUBLIC_VISITOR,)):
        scope = report_scope(_caller(*roles), catalog)
        assert scope.tenant_wide and not scope.users_visible, roles
    # A tester who also holds a role that sees users sees them; the visitor never does.
    assert report_scope(_caller(Role.TESTER, Role.FINANCE_ANALYST), catalog).users_visible
    assert not report_scope(_caller(Role.PUBLIC_VISITOR, Role.TENANT_ADMIN), catalog).users_visible
    for role in (Role.TENANT_ADMIN, Role.FINANCE_ANALYST, Role.SECURITY_AUDITOR):
        assert report_scope(_caller(role), catalog).users_visible, role


async def test_the_tester_opens_the_week_page_tenant_wide(fresh, as_user) -> None:
    async with as_user("u-tester") as client:
        tester = [await client.get(path, params={**WINDOW, **params}) for path, params in WEEK_PAGE]
    async with as_user("u-finance") as client:
        finance = await client.get("/v1/reports/consumption", params={**WINDOW, "group_by": "application"})
    assert [r.status_code for r in tester] == [200, 200, 200], [r.text for r in tester]
    for response in tester:
        scope = response.json()["metadata"]["scope"]
        assert scope["level"] == "tenant" and scope["users_visible"] is False
        assert "usr_" not in response.text
    # The whole tenant: the same applications finance sees.
    assert {r["group"] for r in tester[2].json()["rows"]} == {r["group"] for r in finance.json()["rows"]}


async def test_the_tester_never_sees_the_user_dimension(fresh, as_user) -> None:
    async with as_user("u-tester") as client:
        by_user = await client.get(
            "/v1/reports/consumption",
            params={**WINDOW, "group_by": "application", "user": "usr_0000000000000000"},
        )
        grouped = await client.get("/v1/reports/consumption", params={**WINDOW, "group_by": "user"})
        runs = (await client.get("/v1/reports/runs", params={**WINDOW, "limit": 5})).json()["runs"]
        trace = await client.get(f"/v1/reports/runs/{runs[0]['trace_id']}")
    assert by_user.status_code == grouped.status_code == 403
    assert trace.status_code == 200 and trace.json()["run"]["principal"] == "hidden"
    assert "usr_" not in trace.text


async def test_the_tester_cannot_export_or_resolve_identities(fresh, as_user) -> None:
    export = {"report": "consumption", "group_by": "application", "window": WINDOW, "filters": {}}
    resolve = {"pseudonyms": ["usr_0000000000000000"], "reason": "Investigating a spike"}
    async with as_user("u-tester") as client:
        exported = await client.post("/v1/reports/exports", json=export)
        listed = await client.get("/v1/reports/exports")
        resolved = await client.post("/v1/reports/identities/resolve", json=resolve)
    statuses = [exported.status_code, listed.status_code, resolved.status_code]
    assert statuses == [403, 403, 403], [exported.text, listed.text, resolved.text]
