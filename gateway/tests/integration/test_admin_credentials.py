"""Application keys issued and revoked from the admin console (ADR-0027)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from aiva_gateway.auth.principal import Caller
from aiva_gateway.auth.tokens import pseudonymise
from aiva_gateway.core.enums import Role
from aiva_gateway.core.errors import ConflictError
from aiva_gateway.db.models import ApplicationCredentialRow, AuditLogRow
from aiva_gateway.govern import credentials as keys
from aiva_gateway.govern.credentials import ACTIVE_LIMIT
from tests.conftest import ADMIN, SECOND_ADMIN

APP = "gct-doc-copilot"
OWNER, OWNED = "u-support-owner", "gct-support-chatbot"


def _path(app: str = APP, *rest: str) -> str:
    return "/".join([f"/v1/admin/applications/{app}/credentials", *rest])


@pytest.fixture
def as_user(make_client, user_token):  # type: ignore[no-untyped-def]
    return lambda user: make_client(user_token(user))


@pytest.fixture(autouse=True)
async def clear_limits(services) -> None:  # type: ignore[no-untyped-def]
    # The visitor's public-session limit per source, which other modules' sessions count against.
    async for key in services.redis.scan_iter(match="aiva:public-session:*"):
        await services.redis.delete(key)


async def _issue(as_user, app: str = APP, user: str = ADMIN, **body):  # type: ignore[no-untyped-def]
    async with as_user(user) as client:
        return await client.post(_path(app), json={"label": "SDK smoke test", **body})


async def _revoke(as_user, key_id: str, app: str = APP, user: str = ADMIN):  # type: ignore[no-untyped-def]
    async with as_user(user) as client:
        return await client.post(_path(app, key_id, "revoke"))


async def _me(make_client, credential: str):  # type: ignore[no-untyped-def]
    async with make_client(credential) as client:
        return await client.get("/v1/auth/me")


async def _row(db, key_id: str) -> ApplicationCredentialRow:  # type: ignore[no-untyped-def]
    db.expire_all()
    row = await db.get(ApplicationCredentialRow, key_id)
    assert row is not None
    return row


async def _audit(db, action: str, key_id: str) -> list[AuditLogRow]:  # type: ignore[no-untyped-def]
    rows = await db.execute(
        select(AuditLogRow).where(AuditLogRow.action == action, AuditLogRow.target_id == key_id)
    )
    return list(rows.scalars())


async def test_an_issued_key_is_shown_once_stored_as_an_hmac_and_authenticates(
    as_user, make_client, db, settings
) -> None:  # type: ignore[no-untyped-def]
    issued = await _issue(as_user)
    assert issued.status_code == 201, issued.text
    assert issued.headers["cache-control"] == "no-store"
    body = issued.json()
    credential, meta = body["credential"], body["metadata"]
    key_id, secret = credential.removeprefix("aiva_").split(".", 1)
    assert meta["key_id"] == key_id and meta["active"] and meta["label"] == "SDK smoke test"
    assert meta["created_by"] == pseudonymise(settings.pseudonym_key.get_secret_value(), ADMIN)
    expires = datetime.fromisoformat(meta["expires_at"])
    assert timedelta(days=29, hours=23) < expires - datetime.now(UTC) <= timedelta(days=30)

    row = await _row(db, key_id)
    assert secret not in (row.secret_hmac, row.label, row.created_by) and len(row.secret_hmac) == 64
    [audit] = await _audit(db, "credential.create", key_id)
    assert audit.actor_id == meta["created_by"] and audit.details["application_id"] == APP
    assert secret not in str(audit.details) and credential not in str(audit.details)

    me = await _me(make_client, credential)
    assert me.status_code == 200 and me.json()["application_id"] == APP
    async with as_user(ADMIN) as client:
        listed = await client.get(_path())
    assert listed.status_code == 200 and listed.json()["active_limit"] == ACTIVE_LIMIT
    assert key_id in {c["key_id"] for c in listed.json()["credentials"]}
    assert secret not in listed.text
    await _revoke(as_user, key_id)


async def test_last_use_is_recorded_at_most_hourly(as_user, make_client, db) -> None:  # type: ignore[no-untyped-def]
    credential = (await _issue(as_user)).json()["credential"]
    key_id = credential.removeprefix("aiva_").split(".", 1)[0]
    assert (await _row(db, key_id)).last_used_at is None
    assert (await _me(make_client, credential)).status_code == 200
    first = (await _row(db, key_id)).last_used_at
    assert first is not None
    assert (await _me(make_client, credential)).status_code == 200
    assert (await _row(db, key_id)).last_used_at == first
    await _revoke(as_user, key_id)


async def test_a_revoked_key_stops_at_once_and_revoking_again_changes_nothing(
    as_user, make_client, db
) -> None:  # type: ignore[no-untyped-def]
    credential = (await _issue(as_user)).json()["credential"]
    key_id = credential.removeprefix("aiva_").split(".", 1)[0]
    revoked = await _revoke(as_user, key_id)
    assert revoked.status_code == 200 and revoked.json()["active"] is False
    assert revoked.json()["revoked_at"] and revoked.json()["revoked_by"]
    assert (await _me(make_client, credential)).status_code == 401
    again = await _revoke(as_user, key_id, user=SECOND_ADMIN)
    assert again.status_code == 200 and again.json()["revoked_at"] == revoked.json()["revoked_at"]
    assert again.json()["revoked_by"] == revoked.json()["revoked_by"]
    assert len(await _audit(db, "credential.revoke", key_id)) == 1


async def test_an_expired_key_is_refused(as_user, make_client, db) -> None:  # type: ignore[no-untyped-def]
    credential = (await _issue(as_user, expires_in_days=1)).json()["credential"]
    key_id = credential.removeprefix("aiva_").split(".", 1)[0]
    await db.execute(
        update(ApplicationCredentialRow)
        .where(ApplicationCredentialRow.key_id == key_id)
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await db.commit()
    assert (await _me(make_client, credential)).status_code == 401
    async with as_user(ADMIN) as client:
        listed = (await client.get(_path())).json()["credentials"]
    assert next(c for c in listed if c["key_id"] == key_id)["active"] is False


async def _active(db, app: str) -> int:  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    count = await db.scalar(
        select(func.count())
        .select_from(ApplicationCredentialRow)
        .where(
            ApplicationCredentialRow.application_id == app,
            ApplicationCredentialRow.revoked_at.is_(None),
            (ApplicationCredentialRow.expires_at.is_(None)) | (ApplicationCredentialRow.expires_at > now),
        )
    )
    return int(count or 0)


async def test_an_application_holds_at_most_five_active_keys(as_user, db) -> None:  # type: ignore[no-untyped-def]
    app = "gct-market-intelligence"
    issued: list[str] = []
    while await _active(db, app) < ACTIVE_LIMIT:
        issued.append((await _issue(as_user, app)).json()["metadata"]["key_id"])
    refused = await _issue(as_user, app)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "CREDENTIAL_LIMIT"
    async with as_user(ADMIN) as client:
        full = (await client.get(_path(app))).json()
    assert full["viewer_can_issue"] is False and full["viewer_can_revoke"] is True
    # Revoking one frees a slot.
    assert (await _revoke(as_user, issued[0], app)).status_code == 200
    replacement = await _issue(as_user, app)
    assert replacement.status_code == 201
    issued.append(replacement.json()["metadata"]["key_id"])
    for key_id in issued[1:]:
        await _revoke(as_user, key_id, app)


async def test_concurrent_issues_cannot_pass_the_limit(services, as_user, db) -> None:  # type: ignore[no-untyped-def]
    """The first issue holds the application's row lock until it commits; a second waits for it, then sees
    the full limit and is refused. Without the lock both would commit a fifth key."""
    app = "gct-public-web-assistant"
    issued: list[str] = []
    while await _active(db, app) < ACTIVE_LIMIT - 1:
        issued.append((await _issue(as_user, app)).json()["metadata"]["key_id"])
    admin = Caller(
        kind="user", tenant_id="gct", pseudonym="usr_admin", roles=frozenset({Role.PLATFORM_ADMIN})
    )
    pepper = services.settings.credential_pepper.get_secret_value()
    async with services.sessionmaker() as first, services.sessionmaker() as second:
        await first.begin()
        await second.begin()
        _, row = await keys.issue(first, admin, app, label="first", days=1, pepper=pepper)
        racing = asyncio.create_task(keys.issue(second, admin, app, label="second", days=1, pepper=pepper))
        await asyncio.sleep(0.3)
        assert not racing.done(), "the second issue did not wait for the first one's lock"
        await first.commit()
        with pytest.raises(ConflictError) as refused:
            await racing
        assert refused.value.code == "CREDENTIAL_LIMIT"
        await second.rollback()
    for key_id in [*issued, row.key_id]:
        await _revoke(as_user, key_id, app)


@pytest.mark.parametrize("user", ["u-tester", "u-auditor", OWNER, "u-finance", "u-care-manager"])
async def test_only_administrators_issue_or_revoke(as_user, user: str) -> None:  # type: ignore[no-untyped-def]
    assert (await _issue(as_user, OWNED, user=user)).status_code == 403
    key_id = (await _issue(as_user, OWNED)).json()["metadata"]["key_id"]
    assert (await _revoke(as_user, key_id, OWNED, user=user)).status_code == 403
    assert (await _revoke(as_user, key_id, OWNED)).status_code == 200


async def test_a_visitor_an_application_or_no_credential_never_manages_keys(make_client, app_client) -> None:  # type: ignore[no-untyped-def]
    async with make_client() as client:
        session = await client.post("/v1/auth/public/session")
        anonymous = await client.post(_path(), json={"label": "x"})
    visitor = session.json()["access_token"]
    async with make_client(visitor) as client:
        by_visitor = await client.post(_path(), json={"label": "x"})
        visitor_list = await client.get(_path())
    async with app_client(APP) as client:
        by_application = await client.post(_path(), json={"label": "x"})
    assert anonymous.status_code == 401
    assert by_visitor.status_code == visitor_list.status_code == by_application.status_code == 403


async def test_authorization_comes_before_the_body(as_user) -> None:  # type: ignore[no-untyped-def]
    async with as_user("u-tester") as client:
        unauthorized = await client.post(_path(), json={"label": "", "expires_in_days": 0})
    assert unauthorized.status_code == 403
    for body in (
        {"label": ""},
        {"label": "   "},
        {"label": "x" * 101},
        {"label": "x", "expires_in_days": 0},
        {"label": "x", "expires_in_days": 91},
    ):
        async with as_user(ADMIN) as client:
            assert (await client.post(_path(), json=body)).status_code == 422, body


async def test_reading_keys_is_admins_auditors_and_an_owner_for_its_own(as_user) -> None:  # type: ignore[no-untyped-def]
    for user, manages in ((ADMIN, True), (SECOND_ADMIN, True), ("u-auditor", False)):
        async with as_user(user) as client:
            listed = await client.get(_path(OWNED))
        assert listed.status_code == 200, user
        assert listed.json()["viewer_can_issue"] is manages and listed.json()["viewer_can_revoke"] is manages
    async with as_user(OWNER) as client:
        own, other = await client.get(_path(OWNED)), await client.get(_path(APP))
    assert own.status_code == 200 and other.status_code == 403
    assert own.json()["viewer_can_issue"] is False and own.json()["viewer_can_revoke"] is False
    for user in ("u-tester", "u-finance"):
        async with as_user(user) as client:
            assert (await client.get(_path(OWNED))).status_code == 403, user


async def test_unknown_application_or_another_applications_key_is_not_found(as_user) -> None:  # type: ignore[no-untyped-def]
    key_id = (await _issue(as_user, OWNED)).json()["metadata"]["key_id"]
    assert (await _issue(as_user, "no-such-app")).status_code == 404
    assert (await _revoke(as_user, key_id, APP)).status_code == 404
    assert (await _revoke(as_user, "0000000000000000", OWNED)).status_code == 404
    assert (await _revoke(as_user, key_id, OWNED)).status_code == 200


async def test_the_application_list_shows_keys_only_to_their_readers(
    as_user, make_client, credentials
) -> None:  # type: ignore[no-untyped-def]
    async with make_client() as client:
        visitor = (await client.post("/v1/auth/public/session")).json()["access_token"]
    async with make_client(visitor) as client:
        public = (await client.get("/v1/admin/applications")).json()["applications"]
    assert public and all(app["credentials"] == [] for app in public)
    # The auditor reads every application and every key list; an owner never reads the application list
    # (no `policies:read`), only its own keys, at the credentials route.
    for user in (ADMIN, "u-auditor"):
        async with as_user(user) as client:
            listed = (await client.get("/v1/admin/applications")).json()["applications"]
        apps = {a["id"]: a for a in listed}
        assert apps[OWNED]["credentials"] and apps[APP]["credentials"], user
