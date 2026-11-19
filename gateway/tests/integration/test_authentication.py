"""Authentication refusals (ADR-0007): a revoked or forged credential and a token without a matching principal
are all 401 AUTHENTICATION_REQUIRED."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import update

from aiva_gateway.auth.credentials import parse_credential
from aiva_gateway.auth.tokens import mint_user_token
from aiva_gateway.catalog.seed import create_application_credential
from aiva_gateway.db.models import ApplicationCredentialRow
from tests.conftest import PEERS, chat_body

APP = "gct-support-chatbot"


async def test_a_revoked_application_credential_is_refused(services, make_client) -> None:  # type: ignore[no-untyped-def]
    # A credential of its own: the session-scoped `credentials` fixture is shared by other tests.
    async with services.sessionmaker() as session, session.begin():
        credential = await create_application_credential(services, session, APP, "revocation test")
    parsed = parse_credential(credential)
    assert parsed is not None
    async with make_client(credential, peer=PEERS[APP]) as client:
        before = await client.post("/v1/ai/chat", json=chat_body("Explain roaming charges."))
    async with services.sessionmaker() as session, session.begin():
        await session.execute(
            update(ApplicationCredentialRow)
            .where(ApplicationCredentialRow.key_id == parsed[0])
            .values(revoked_at=datetime.now(UTC))
        )
    async with make_client(credential, peer=PEERS[APP]) as client:
        after = await client.post("/v1/ai/chat", json=chat_body("Explain roaming charges."))
    assert before.status_code == 200, before.text
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


async def test_a_credential_with_the_wrong_secret_is_refused(credentials, make_client) -> None:  # type: ignore[no-untyped-def]
    forged = credentials[APP].rsplit(".", 1)[0] + ".not-the-secret"
    async with make_client(forged) as client:
        response = await client.get("/v1/admin/approvals")
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("user_id", "tenant_id", "ttl_seconds"),
    [
        ("u-tester", "another-tenant", 3600),  # the token's tenant is not the principal's
        ("u-deleted-or-unknown", "gct", 3600),  # no such principal
        ("u-tester", "gct", -60),  # expired
    ],
    ids=["foreign-tenant", "unknown-principal", "expired"],
)
async def test_user_tokens_that_do_not_match_a_principal_are_refused(
    services, make_client, user_id: str, tenant_id: str, ttl_seconds: int
) -> None:  # type: ignore[no-untyped-def]
    token = mint_user_token(services.settings, user_id=user_id, tenant_id=tenant_id, ttl_seconds=ttl_seconds)
    async with make_client(token) as client:
        response = await client.get("/v1/admin/approvals")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
