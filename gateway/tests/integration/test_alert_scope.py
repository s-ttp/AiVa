"""Row-level scope of FinOps alerts (LLD §21.8): owners and department managers see alerts about their
applications, including a user's anomaly within one of them (the user stays a pseudonym)."""

from datetime import UTC, datetime, timedelta

from aiva_gateway.core.enums import Provenance
from aiva_gateway.core.ids import ulid
from aiva_gateway.finops.alerts import AlertDraft, record_alerts
from aiva_gateway.finops.scopes import Bucket


def draft(marker: str, scope_type: str, scope_id: str, application_id: str | None = None) -> AlertDraft:
    return AlertDraft(
        tenant_id="gct",
        kind="unusual_user_consumption" if scope_type == "user" else "threshold",
        message=f"{marker} {scope_type} {scope_id}",
        provenance=Provenance.SYNTHETIC,
        bucket=Bucket(scope_type, scope_id, "hour", "2026-10-15T06"),
        application_id=application_id,
    )


async def visible(make_client, user_token, user: str, marker: str, since: datetime) -> set[str]:
    async with make_client(user_token(user)) as client:
        response = await client.get(
            "/v1/admin/budgets/alerts", params={"since": since.isoformat(), "limit": 500}
        )
    assert response.status_code == 200, response.text
    return {
        a["message"].removeprefix(f"{marker} ") for a in response.json()["alerts"] if marker in a["message"]
    }


def test_application_is_derived_from_the_scope() -> None:
    assert draft("m", "application", "gct-support-chatbot").row().application_id == "gct-support-chatbot"
    assert draft("m", "request", "gct-noc-assistant").row().application_id == "gct-noc-assistant"
    assert draft("m", "workflow", "gct-incident-agent/wf_1").row().application_id == "gct-incident-agent"
    assert draft("m", "tenant", "gct").row().application_id is None
    assert draft("m", "user", "usr_a", "gct-support-chatbot").row().application_id == "gct-support-chatbot"


async def test_owners_and_managers_see_their_applications_users(services, make_client, user_token) -> None:
    marker, since = f"scope-{ulid()}", datetime.now(UTC) - timedelta(seconds=1)
    await record_alerts(
        services.sessionmaker,
        [
            draft(marker, "application", "gct-support-chatbot"),
            draft(marker, "user", "usr_care", "gct-support-chatbot"),  # a user's anomaly in a care app
            draft(marker, "user", "usr_noc", "gct-noc-assistant"),  # a user's anomaly in a NOC app
            draft(marker, "user", "usr_none"),  # no application: tenant-wide roles only
            draft(marker, "tenant", "gct"),
        ],
    )
    care = {"application gct-support-chatbot", "user usr_care"}
    assert await visible(make_client, user_token, "u-support-owner", marker, since) == care
    assert await visible(make_client, user_token, "u-care-manager", marker, since) == care
    assert await visible(make_client, user_token, "u-noc-manager", marker, since) == {"user usr_noc"}
    assert await visible(make_client, user_token, "u-finance", marker, since) == care | {
        "user usr_noc",
        "user usr_none",
        "tenant gct",
    }
