"""Passcode sign-in for the owner and demo users (ADR-0019), and dev sign-in kept to local and test."""

import json

import pytest
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import select

from aiva_gateway.auth import demo_sign_in
from aiva_gateway.auth.authenticate import PUBLIC_VISITOR_ID
from aiva_gateway.db.models import AuditLogRow
from aiva_gateway.services import Services
from tests.conftest import with_overrides

PASSCODE = "synthetic-demo-passcode-0042"


def with_passcode(services: Services, **update: object) -> Services:
    settings = services.settings.model_copy(update={"demo_passcode": SecretStr(PASSCODE), **update})
    return with_overrides(services, settings=settings)


@pytest.fixture(autouse=True)
async def clear_limits(services: Services) -> None:
    async for key in services.redis.scan_iter(match="aiva:demo-sign-in:*"):
        await services.redis.delete(key)


async def _sign_in(
    make_client, svc, passcode: str = PASSCODE, user_id: str = "u-tester", peer: str = "127.0.0.1"
):  # type: ignore[no-untyped-def]
    async with make_client(svc=svc, peer=peer) as client:
        return await client.post("/v1/auth/demo/token", json={"user_id": user_id, "passcode": passcode})


async def _audit(db, action: str) -> AuditLogRow | None:  # type: ignore[no-untyped-def]
    rows = await db.scalars(
        select(AuditLogRow).where(AuditLogRow.action == action).order_by(AuditLogRow.id.desc())
    )
    return rows.first()


async def test_passcode_sign_in_is_off_without_a_passcode(make_client) -> None:  # type: ignore[no-untyped-def]
    async with make_client() as client:
        options = (await client.get("/v1/auth/sign-in-options")).json()
        users = await client.get("/v1/auth/demo/users")
        token = await client.post("/v1/auth/demo/token", json={"user_id": "u-tester", "passcode": "anything"})
    assert options["passcode"] is False
    assert users.status_code == token.status_code == 404
    assert token.json()["error"]["code"] == "DEMO_SIGN_IN_DISABLED"


async def test_the_passcode_signs_in_a_demo_user(services, make_client, db) -> None:  # type: ignore[no-untyped-def]
    svc = with_passcode(services)
    async with make_client(svc=svc) as client:
        options = (await client.get("/v1/auth/sign-in-options")).json()
        users = {u["user_id"] for u in (await client.get("/v1/auth/demo/users")).json()}
    signed_in = await _sign_in(make_client, svc)
    assert options["passcode"] is True and "u-tester" in users and PUBLIC_VISITOR_ID not in users
    assert signed_in.status_code == 200, signed_in.text
    async with make_client(signed_in.json()["access_token"]) as client:
        assert (await client.get("/v1/auth/me")).json()["user_id"] == "u-tester"
    row = await _audit(db, "auth.demo_sign_in")
    assert row is not None and row.actor_kind == "user" and row.actor_id.startswith("usr_")
    assert row.details["source"].startswith("cli_") and PASSCODE not in json.dumps(row.details)


async def test_a_wrong_passcode_is_refused_and_audited_without_it(services, make_client, db) -> None:  # type: ignore[no-untyped-def]
    wrong = await _sign_in(make_client, with_passcode(services), passcode="guess-" + PASSCODE[:8])
    assert wrong.status_code == 401 and wrong.json()["error"]["code"] == "DEMO_SIGN_IN_FAILED"
    assert "guess-" not in wrong.text and PASSCODE not in wrong.text
    row = await _audit(db, "auth.demo_sign_in_refused")
    assert row is not None and row.details["outcome"] == "wrong-passcode"
    assert "guess-" not in json.dumps(row.details) and row.actor_id.startswith("cli_")


async def test_repeated_failures_lock_the_source_out(services, make_client) -> None:  # type: ignore[no-untyped-def]
    svc = with_passcode(services)
    peer = "10.77.0.1"
    statuses = [
        (await _sign_in(make_client, svc, passcode="wrong-passcode", peer=peer)).status_code
        for _ in range(demo_sign_in.FAILURES_BEFORE_LOCKOUT)
    ]
    locked = await _sign_in(make_client, svc, peer=peer)  # even the right passcode, while locked
    elsewhere = await _sign_in(make_client, svc, peer="10.77.0.2")
    assert statuses == [401] * demo_sign_in.FAILURES_BEFORE_LOCKOUT
    assert locked.status_code == 429 and locked.json()["error"]["code"] == "DEMO_SIGN_IN_LOCKED"
    assert 0 < int(locked.headers["Retry-After"]) <= demo_sign_in.LOCKOUT_SECONDS
    assert elsewhere.status_code == 200


async def test_attempts_are_capped_tenant_wide(services, make_client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(demo_sign_in, "ATTEMPTS_PER_MINUTE", 2)
    svc = with_passcode(services)
    statuses = [(await _sign_in(make_client, svc, peer=f"10.78.0.{i}")).status_code for i in range(1, 4)]
    assert statuses == [200, 200, 429]


async def test_without_redis_no_attempt_is_verified(services, make_client) -> None:  # type: ignore[no-untyped-def]
    down = Redis.from_url("redis://127.0.0.1:9/0", socket_connect_timeout=0.2)
    try:
        response = await _sign_in(make_client, with_overrides(with_passcode(services), redis=down))
    finally:
        await down.aclose()
    assert response.status_code == 503


async def test_unknown_users_and_the_visitor_are_not_signed_in(services, make_client) -> None:  # type: ignore[no-untyped-def]
    svc = with_passcode(services)
    unknown = await _sign_in(make_client, svc, user_id="u-nobody")
    visitor = await _sign_in(make_client, svc, user_id=PUBLIC_VISITOR_ID)
    assert unknown.status_code == visitor.status_code == 404


async def test_dev_sign_in_is_refused_on_a_deployed_gateway_even_if_enabled(services, make_client) -> None:  # type: ignore[no-untyped-def]
    # `model_copy` skips the start-up validator; the endpoints check again at request time.
    settings = services.settings.model_copy(update={"dev_sign_in": True, "environment": "demo"})
    async with make_client(svc=with_overrides(services, settings=settings)) as client:
        options = (await client.get("/v1/auth/sign-in-options")).json()
        users = await client.get("/v1/auth/dev/users")
        token = await client.post("/v1/auth/dev/token", json={"user_id": "u-platform-admin"})
    assert options["dev"] is False
    assert users.status_code == token.status_code == 404
    assert token.json()["error"]["code"] == "DEV_SIGN_IN_DISABLED"
